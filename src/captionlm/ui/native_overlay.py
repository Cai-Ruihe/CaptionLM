"""Native NSPanel-based subtitle overlay (PyObjC).

Why this exists (2026-05-06): after 7+ rounds of failed attempts to get
reliable click-through on a Qt overlay, the user gave up on the Qt path
and asked to try the macOS-native NSPanel approach. System dialogs (TCC
alerts, etc.) achieve "always-on-top + clickable + non-intrusive" via
Cocoa primitives; Qt's wrapping has too many edge cases.

This module bypasses Qt's QWidget abstraction entirely. The overlay is
an NSPanel with:
  - NSWindowStyleMaskNonactivatingPanel — doesn't steal keyboard focus
  - NSScreenSaverWindowLevel — above all normal app windows
  - setIgnoresMouseEvents:YES — TRUE click-through to apps below
  - canBecomeKeyWindow/canBecomeMainWindow override → NO
  - collectionBehavior with canJoinAllSpaces + fullScreenAuxiliary

Content is an NSStackView with NSTextField children for each row
(5 history + current original + current translation). Pure Cocoa
rendering, no Qt paint involved.

Settings access goes through the tray icon's Control Panel menu item
(see captionlm.ui.tray_icon). The overlay itself has NO interactive
widgets — that's the whole point.

API mirrors SubtitleOverlay so app.py can swap implementations:
  - update_subtitle(orig, trans)
  - on_utterance_finalized()
  - clear_history()
  - apply_settings(settings)
  - show() / hide() / toggle_visibility()
  - show_rate_limit_warning(provider)
  - signals: quit_requested, settings_changed, start_stop_clicked,
             show_settings_panel (kept for app.py compat — these will
             never fire from the overlay since there are no buttons,
             they exist only because app.py connects them at startup)
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal, QTimer

from captionlm.config.settings import Settings

logger = logging.getLogger(__name__)


# Try PyObjC imports. If unavailable, NativeSubtitleOverlay creation will
# raise — app.py is expected to fall back to the legacy Qt overlay in
# that case.
try:
    import objc  # noqa: F401
    from AppKit import (
        NSPanel,
        NSScreenSaverWindowLevel,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
        NSWindowStyleMaskResizable,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSBackingStoreBuffered,
        NSColor,
        NSFont,
        NSScreen,
        NSView,
        NSTextField,
        NSStackView,
        NSScrollView,
        NSUserInterfaceLayoutOrientationVertical,
        NSButton,
        NSImage,
        NSTrackingArea,
        NSImageSymbolConfiguration,
        NSMutableParagraphStyle,
        NSAttributedString,
        NSMutableAttributedString,
        NSParagraphStyleAttributeName,
        NSFontAttributeName,
        NSForegroundColorAttributeName,
        NSTextAlignmentCenter,
    )
    from Foundation import NSMakeRect, NSObject
    PYOBJC_AVAILABLE = True
    _IMPORT_ERROR = None
except ImportError as e:
    PYOBJC_AVAILABLE = False
    _IMPORT_ERROR = e

def _hex_to_rgb_floats(hex_str: str) -> tuple[float, float, float]:
    """Parse a #RRGGBB hex string to three 0-1 floats. Falls back to
    white on parse failure so we never crash the overlay on a bad
    settings value."""
    s = (hex_str or "").lstrip("#").strip()
    if len(s) != 6:
        return (1.0, 1.0, 1.0)
    try:
        r = int(s[0:2], 16) / 255.0
        g = int(s[2:4], 16) / 255.0
        b = int(s[4:6], 16) / 255.0
        return (r, g, b)
    except Exception:
        return (1.0, 1.0, 1.0)


# NSTextAlignment value — pulled from AppKit rather than hardcoded
# because the enum was renumbered in macOS 10.12 (Sierra) to match
# UITextAlignment:
#   Pre-10.12: left=0, right=1, center=2, justified=3, natural=4
#   10.12+:    left=0, center=1, right=2, justified=3, natural=4
# Hardcoding "2" gave us RIGHT alignment on modern macOS instead of
# center — this was the root cause of three rounds of "wrapped text
# is not centered" reports (user saw text actually right-aligned
# after wrap, 2026-05-13). Always import the symbol.
_NS_TEXT_ALIGNMENT_CENTER = NSTextAlignmentCenter


def _set_centered_text(field, text: str) -> None:
    """Set an NSTextField's text with paragraph-style center alignment
    explicitly applied via NSMutableAttributedString.addAttribute over
    the full text range.

    Why THIS particular construction (2026-05-13, iteration #3):
    earlier attempts (cell.setAlignment, then initWithString_attributes_
    dict) failed visibly for the user. The most reliable form of
    "this string MUST render centered" in Cocoa is:

      1. Build a mutable attributed string from the raw text
      2. addAttribute:NSParagraphStyleAttributeName range:fullRange
         with a paragraph style whose alignment is center
      3. setAttributedStringValue_ on the field

    Going through addAttribute explicitly (rather than passing a
    Python dict to initWithString:attributes:) bypasses any PyObjC
    dict-conversion quirks that might silently drop the paragraph
    style attribute. The full-range NSRange ensures every glyph
    inherits the center alignment, including in wrapped multi-line
    layouts.
    """
    # NOTE: we always go through the attributed-string path now —
    # even for empty text — so the field stays in attributed-mode
    # consistently. Mixing setStringValue_ and setAttributedStringValue_
    # was a potential source of state confusion in earlier attempts.
    try:
        para = NSMutableParagraphStyle.alloc().init()
        para.setAlignment_(_NS_TEXT_ALIGNMENT_CENTER)
        para.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
        mut = NSMutableAttributedString.alloc().initWithString_(text or "")
        full_range = (0, len(text or ""))
        if full_range[1] > 0:
            mut.addAttribute_value_range_(
                NSParagraphStyleAttributeName, para, full_range,
            )
            try:
                mut.addAttribute_value_range_(
                    NSFontAttributeName, field.font(), full_range,
                )
                mut.addAttribute_value_range_(
                    NSForegroundColorAttributeName, field.textColor(), full_range,
                )
            except Exception:
                # Font/color failures are non-fatal; field falls back
                # to its default font/color which is fine.
                pass
        field.setAttributedStringValue_(mut)
    except Exception:
        # Last-resort fallback to plain string.
        try:
            field.setStringValue_(text or "")
        except Exception:
            pass
    # Belt-and-suspenders alignment + redraw, same as iteration #2.
    try:
        field.setAlignment_(_NS_TEXT_ALIGNMENT_CENTER)
        cell = field.cell()
        if cell is not None:
            cell.setAlignment_(_NS_TEXT_ALIGNMENT_CENTER)
        field.invalidateIntrinsicContentSize()
        field.setNeedsDisplay_(True)
    except Exception:
        pass


def _make_content_view_class():
    """NSView subclass whose setFrameSize_ override reacts to window
    resizes by updating each subtitle text field's preferredMaxLayoutWidth.

    Why this exists (user feedback 2026-05-06): without an explicit max
    layout width, NSTextField uses its content's natural width as
    intrinsic size, so a long subtitle expands horizontally and drags
    the NSStackView + NSPanel wider with it. By telling the field
    "your max layout width is N pixels", the field wraps text once
    its rendered width hits N and grows vertically instead — keeping
    the window at the user's chosen width.

    The content view tracks all subtitle fields (history rows + current
    orig + current trans) so the wrap recomputes for each on resize.
    """

    class ContentView(NSView):
        def setFrameSize_(self, new_size):
            objc.super(ContentView, self).setFrameSize_(new_size)
            self.applyWrapWidths()

        # @objc.python_method: tells PyObjC this is a Python-only method,
        # NOT a Cocoa selector. Without it (or with underscores in the
        # name) PyObjC treats every underscore as a `:` in the selector
        # — `_apply_wrap_widths_` was getting mapped to `_apply:wrap:widths:`
        # and PyObjC threw "expects 3 arguments". camelCase name + the
        # decorator = unambiguous Python-only method.
        @objc.python_method
        def applyWrapWidths(self):
            fields = getattr(self, "_wrap_fields", None)
            if not fields:
                return
            wrap_width = max(60.0, float(self.frame().size.width) - 32.0)
            for tf in fields:
                try:
                    tf.setPreferredMaxLayoutWidth_(wrap_width)
                    tf.invalidateIntrinsicContentSize()
                except Exception:
                    pass

    return ContentView


def _make_panel_class():
    """Build the NSPanel subclass lazily so module import works even
    without PyObjC available."""

    class NonKeyPanel(NSPanel):
        """NSPanel that explicitly refuses to become key/main window.

        Without these overrides, even a NonactivatingPanel can grab
        keyboard focus under some circumstances, which would interrupt
        the user's typing in other apps.
        """

        def canBecomeKeyWindow(self):
            return False

        def canBecomeMainWindow(self):
            return False

    return NonKeyPanel


def _make_hover_button_class():
    """NSButton subclass that lightens its background on mouse hover.

    macOS HIG-style inline action button: no visible bezel by default,
    a soft translucent-white background appears on cursor entry, fades
    on exit. Achieves the "按钮变成浅色" hover feedback the user asked
    for, identical to the inline action affordances Apple's own apps
    use (Notes, Reminders, etc.).
    """

    # NSTrackingArea options flags — pulled from AppKit headers.
    NS_TRACKING_MOUSE_ENTERED_EXITED = 1
    NS_TRACKING_ACTIVE_ALWAYS = 128
    NS_TRACKING_IN_VISIBLE_RECT = 512

    # Tint colors for the icon-only hover effect (no bg layer).
    # Default = dim white; hover = full white. This avoids the "bg
    # shape doesn't match icon shape" perception issue that 4 rounds
    # of bg/sublayer fixes couldn't resolve — by NOT rendering any
    # bg shape, there is nothing to misalign.
    _DEFAULT_TINT = NSColor.colorWithWhite_alpha_(0.72, 1.0)
    _HOVER_TINT = NSColor.colorWithWhite_alpha_(1.0, 1.0)

    class HoverButton(NSButton):
        """NSButton that brightens its icon on hover via
        setContentTintColor_. No bg layer, no shape — just the icon
        itself goes from dim to bright. Apple's own system apps
        (Notes, Reminders, Finder inline actions) use this exact
        pattern for transient action buttons."""

        def initWithFrame_(self, frame):
            self = objc.super(HoverButton, self).initWithFrame_(frame)
            if self is None:
                return None
            self._tracking_area = None
            # Set default dim tint at construction.
            try:
                self.setContentTintColor_(_DEFAULT_TINT)
            except Exception:
                pass
            self._install_tracking()
            return self

        def _install_tracking(self):
            if self._tracking_area is not None:
                self.removeTrackingArea_(self._tracking_area)
            opts = (
                NS_TRACKING_MOUSE_ENTERED_EXITED
                | NS_TRACKING_ACTIVE_ALWAYS
                | NS_TRACKING_IN_VISIBLE_RECT
            )
            self._tracking_area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                self.bounds(), opts, self, None,
            )
            self.addTrackingArea_(self._tracking_area)

        def updateTrackingAreas(self):
            self._install_tracking()
            objc.super(HoverButton, self).updateTrackingAreas()

        def mouseEntered_(self, event):
            try:
                self.setContentTintColor_(_HOVER_TINT)
            except Exception:
                pass

        def mouseExited_(self, event):
            try:
                self.setContentTintColor_(_DEFAULT_TINT)
            except Exception:
                pass

    return HoverButton


def _sf_symbol(name: str, point_size: float = 16.0, weight: int = 5):
    """Return an NSImage for the given SF Symbol name, sized to fill
    a typical 32-pixel button. SF Symbols default to ~13pt which looks
    tiny inside a 32×32 button — the hover bg appears to "miss" the
    icon (user feedback: "变亮部分和图标没有对齐"). NSImageSymbolConfiguration
    overrides the rendered size.

    `weight`: 1=Ultralight … 5=Medium … 7=Bold … 9=Black. Medium reads well.
    """
    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, None,
        )
        if img is None:
            return None
        config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            point_size, weight,
        )
        configured = img.imageWithSymbolConfiguration_(config)
        return configured if configured is not None else img
    except Exception:
        return None


def _make_button_target_class():
    """Lazily build the NSObject subclass used as target for NSButton
    actions. PyObjC requires the class to be importable from a real
    NSObject subclass; building it inside a function keeps the module
    importable when PyObjC isn't installed."""

    class _OverlayButtonTarget(NSObject):
        """Target for NSButton actions. Holds a reference to the
        overlay so Python signals can be emitted in response to clicks.
        Methods named with a trailing underscore become Objective-C
        selectors ending in ':' — Cocoa's button action calls them."""

        def initWithOverlay_(self, overlay):
            self = objc.super(_OverlayButtonTarget, self).init()
            if self is None:
                return None
            self._overlay = overlay
            return self

        def quitClicked_(self, sender):
            logger.info("Overlay × button clicked")
            self._overlay.quit_requested.emit()

        def settingsClicked_(self, sender):
            logger.info("Overlay ⚙ button clicked")
            self._overlay.show_settings_panel.emit()

        def startStopClicked_(self, sender):
            logger.info("Overlay start/stop button clicked")
            self._overlay.start_stop_clicked.emit()

        def logoClicked_(self, sender):
            """Open the GitHub project page in the user's default browser."""
            logger.info("Overlay logo clicked — opening GitHub page")
            try:
                from Foundation import NSURL
                from AppKit import NSWorkspace
                url = NSURL.URLWithString_(
                    "https://github.com/Cai-Ruihe/CaptionLM"
                )
                NSWorkspace.sharedWorkspace().openURL_(url)
            except Exception as e:
                logger.warning("Failed to open GitHub URL: %s", e)

    return _OverlayButtonTarget


# Trailing punctuation set, mirrors SessionRecorder._TRAILING_PUNCT and
# the Qt overlay constant — keeps dedup behavior consistent.
_TRAILING_PUNCT = "。、？！?!.,…．・;；:： "


class NativeSubtitleOverlay(QObject):
    """Drop-in replacement for SubtitleOverlay using NSPanel + PyObjC."""

    # Same signal surface as SubtitleOverlay so app.py can connect them
    # without conditionals. They simply never fire from this overlay
    # since the panel has no interactive widgets.
    settings_changed = Signal()
    start_stop_clicked = Signal()
    quit_requested = Signal()
    show_settings_panel = Signal()

    # Scrollable history capacity — pre-allocates this many NSTextField
    # rows up-front so the scroll view can hold the entire session's
    # committed translations (up to _MAX_COMMITTED). Unused rows are
    # hidden via NSStackView setHidden_, so the panel stays compact
    # at session start and grows only the scroll content as the user
    # accumulates history. User request (2026-05-12): "可查历史记录
    # 变成这个session中所有的翻译（或者是非常长，比如50条）".
    _HISTORY_ROWS = 50
    _MAX_COMMITTED = 50
    _COMMIT_SILENCE_MS = 2000

    def __init__(self, settings: Settings):
        super().__init__()
        if not PYOBJC_AVAILABLE:
            raise RuntimeError(
                f"PyObjC required for NativeSubtitleOverlay (import error: "
                f"{_IMPORT_ERROR}). Install with: "
                f"pip install pyobjc-core pyobjc-framework-Cocoa"
            )

        self.settings = settings
        self._visible = True

        # State for subtitle pipeline (redesigned 2026-05-13)
        self._committed_pairs: list[tuple[str, str]] = []
        # Live-area accumulators — accumulate chunks within an utterance
        # so the live display shows the running utterance translation
        # in full, while each chunk lands in history as its own short
        # entry. User feedback: "每一句还是太长，长度能变成现在1/3就好了"
        # — we now make history per-chunk; live-area still shows
        # the connected per-utterance content for context.
        self._current_orig: str = ""
        self._current_trans: str = ""
        # True when the next incoming chunk should START a fresh
        # utterance in the live area (clear the accumulator). Set by
        # on_utterance_finalized() (STT is_final) and by the 2s silence
        # timer. Replaces the older _current_finalized + _current_pushed
        # pair (no need to defer pushes since each chunk is pushed
        # individually now).
        self._next_is_new_utterance: bool = True

        # Streaming-translator mode flag. When True (engine like Qwen
        # LiveTranslate that produces (orig, trans, is_final) end-to-end),
        # the overlay TRUSTS the engine's is_final boundary as the SOLE
        # commit trigger — no sentence-end punct heuristic, no 2s silence
        # timer commit. Empirically (log 2026-05-14): Qwen partial deltas
        # carry sentence-ending punct on every update, so the heuristic
        # fires constantly and pushes fragmented variants to history.
        # See on_utterance_finalized for the authoritative commit path.
        self._streaming_translator_mode: bool = False

        # Cocoa objects (set in _build_panel)
        self._panel = None
        self._content_view = None
        self._stack_view = None
        self._history_fields: list = []
        self._orig_field = None
        self._trans_field = None

        # 2-second silence timer as fallback for utterance commit
        # (utterance_finalized signal is the primary commit trigger).
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(self._COMMIT_SILENCE_MS)
        self._commit_timer.timeout.connect(self._commit_current_to_history)

        self._build_panel()
        self._refresh_history_fields()

    # ──────────────────────────────────────────────────────────────
    # Cocoa panel construction
    # ──────────────────────────────────────────────────────────────

    def _build_panel(self):
        # Bottom-center placement on the main screen.
        screen_frame = NSScreen.mainScreen().visibleFrame()
        screen_w = float(screen_frame.size.width)
        width = min(800.0, screen_w - 100.0)
        # Height is generous enough to fit 5 history rows + orig + trans
        # at default sizes when fully populated. Hidden rows collapse
        # via NSStackView's setHidden_ mechanism — the visual content
        # shrinks naturally when history isn't filled yet.
        height = 200.0
        x = (screen_w - width) / 2.0 + float(screen_frame.origin.x)
        # macOS coordinates are bottom-left origin → y from bottom.
        y = 100.0 + float(screen_frame.origin.y)

        rect = NSMakeRect(x, y, width, height)
        # Resizable: macOS allows edge drag to resize even for borderless
        # panels when this mask is set. Cursor changes near edges; no
        # visible resize chrome, which keeps the clean look.
        style = (
            NSWindowStyleMaskBorderless
            | NSWindowStyleMaskNonactivatingPanel
            | NSWindowStyleMaskResizable
        )

        PanelCls = _make_panel_class()
        panel = PanelCls.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False,
        )
        # Bounds for resize: don't let user shrink past readability or
        # grow beyond the screen width.
        panel.setMinSize_((360.0, 80.0))
        panel.setMaxSize_((screen_w - 40.0, 800.0))
        panel.setLevel_(NSScreenSaverWindowLevel)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        # Click-through is OFF — user explicitly said they prefer
        # catching clicks for drag / future hover-menu functionality
        # over the "clicks pass to YouTube" behavior. The overlay can
        # be dragged out of the way if it covers important YouTube UI.
        panel.setIgnoresMouseEvents_(False)
        # Drag-anywhere-to-move. NSPanel honors this on its background
        # area (the rounded dark bg in our case) — user clicks and
        # drags the chrome, the panel follows.
        panel.setMovableByWindowBackground_(True)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setReleasedWhenClosed_(False)

        # Content view: ContentView subclass (overrides setFrameSize_
        # to propagate the panel's current width to each subtitle text
        # field's preferredMaxLayoutWidth — keeps text wrapping inside
        # the window even when individual lines are very long).
        ContentViewCls = _make_content_view_class()
        content = ContentViewCls.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        content.setAutoresizingMask_(2 | 16)  # WidthSizable | HeightSizable
        content.setWantsLayer_(True)
        content._wrap_fields = []  # filled below as fields are created
        # Background color + opacity from settings (user-customizable
        # via control panel). Alpha = overlay_opacity / 100. Default
        # color is the dark navy that's been the panel's identity since
        # 2026-05-06; users who want a different vibe can pick a new
        # color or drop opacity to 0 for fully transparent bg.
        bg_r, bg_g, bg_b = _hex_to_rgb_floats(self.settings.overlay_bg_color)
        bg_alpha = max(0.0, min(1.0, self.settings.overlay_opacity / 100.0))
        bg_color = NSColor.colorWithRed_green_blue_alpha_(bg_r, bg_g, bg_b, bg_alpha)
        layer = content.layer()
        layer.setBackgroundColor_(bg_color.CGColor())
        layer.setCornerRadius_(16.0)
        layer.setMasksToBounds_(True)
        # Stash the layer ref so apply_settings() can re-color it
        # without rebuilding the whole content view.
        self._content_layer = layer

        # Two-section layout (2026-05-06 redesign):
        #
        #   ┌──────────────────────────────────────┐
        #   │ ┌──────────────────────────────────┐ │  ← top: history NSScrollView
        #   │ │ history row 1                    │ │     (NSStackView inside)
        #   │ │ history row 2                    │ │     scrolls when content
        #   │ │ ...                              │ │     exceeds visible area
        #   │ │ history row N                    │ │
        #   │ └──────────────────────────────────┘ │
        #   │ ─────────────────────────────────── │  ← thin divider
        #   │ current orig (small)                 │  ← bottom: live area, fixed
        #   │ current translation (BIG bold)       │     bottom: always visible
        #   └──────────────────────────────────────┘
        #
        # User feedback: long wrapped sentences made 5 history entries
        # push the live row past screen height. Now history scrolls
        # within its own region and live stays pinned at the bottom.
        # Stack alignment = CenterX → wrapped lines render centered.
        _CENTER_X = 9  # NSLayoutAttributeCenterX

        # Live area (bottom, fixed): inside its own small stack so its
        # intrinsic height is determined by orig + trans content.
        live_stack = NSStackView.alloc().init()
        live_stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        live_stack.setSpacing_(4.0)
        live_stack.setAlignment_(_CENTER_X)

        # Live area: cap at 3 lines so a single long translation
        # doesn't blow up the panel height (Qwen sometimes returns
        # 130+ chars per utterance, which wraps to 5+ lines).
        # Truncation handled by lineBreakMode=byTruncatingTail in
        # cell setup below.
        self._orig_field = self._make_text_field(
            font_size=int(self.settings.original_font_size),
            alpha=0.78, bold=False, max_lines=3,
        )
        live_stack.addArrangedSubview_(self._orig_field)
        content._wrap_fields.append(self._orig_field)

        self._trans_field = self._make_text_field(
            font_size=int(self.settings.translated_font_size),
            alpha=0.98, bold=True, max_lines=3,
        )
        live_stack.addArrangedSubview_(self._trans_field)
        content._wrap_fields.append(self._trans_field)

        # NOTE (2026-05-13): previously had widthAnchor constraints
        # pinning each field to live_stack.widthAnchor with `==`. They
        # created a circular layout dep (field.width == stack.width,
        # stack.width = max(children intrinsic)) and AutoLayout silently
        # degraded.
        # NOTE (2026-05-14): widthAnchor ≤ content.widthAnchor() must
        # be activated AFTER the view hierarchy is established (i.e.
        # after content.addSubview_(outer_stack) is called below) —
        # otherwise the anchors have no common ancestor and constraint
        # activation throws NSGenericException. See _pin_live_max_width
        # below — called at the END of _build_panel.

        # History stack: inside a scroll view so it can scroll
        # independently when content overflows.
        history_stack = NSStackView.alloc().init()
        history_stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        history_stack.setSpacing_(4.0)
        history_stack.setAlignment_(_CENTER_X)

        history_size = max(8, int(self.settings.translated_font_size) - 2)
        for i in range(self._HISTORY_ROWS):
            field = self._make_text_field(
                font_size=history_size, alpha=0.55, bold=False,
                selectable=True,  # user can drag-select + Cmd+C copy history
            )
            field.setHidden_(True)
            history_stack.addArrangedSubview_(field)
            self._history_fields.append(field)
            content._wrap_fields.append(field)

        # Scroll view wraps the history stack.
        history_scroll = NSScrollView.alloc().init()
        history_scroll.setHasVerticalScroller_(True)
        history_scroll.setHasHorizontalScroller_(False)
        history_scroll.setAutohidesScrollers_(True)
        history_scroll.setBorderType_(0)  # NSNoBorder
        history_scroll.setDrawsBackground_(False)
        # Document view (history_stack) needs an explicit width binding;
        # without it the NSScrollView's content size is 0 and nothing
        # renders (user feedback: "没有看到历史记录的部分"). Pin width
        # to the scroll's clipView so the stack fills horizontally and
        # only scrolls vertically.
        history_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
        history_scroll.setDocumentView_(history_stack)
        try:
            clip = history_scroll.contentView()
            # Defensive: NSClipView (the scroll view's inner viewport) has
            # its own `drawsBackground` property, independent of the
            # NSScrollView's. Empirically (2026-05-16): on Python 3.13 +
            # the PyObjC version py2app bundled into v0.1.0's .app, the
            # default was True → opaque system white showed through behind
            # the scrollbar against our translucent overlay window ("白色
            # 背景好丑" — user feedback). On Python 3.14 + current PyObjC,
            # the default is False so nothing visible breaks, but relying
            # on an Apple-side default that quietly varies across SDK /
            # PyObjC versions is fragile. Set it explicitly so behavior
            # is consistent regardless of which Python or PyObjC the app
            # is bundled with (especially after py2app freezes the runtime
            # at release time).
            clip.setDrawsBackground_(False)
            history_stack.widthAnchor().constraintEqualToAnchor_(
                clip.widthAnchor()
            ).setActive_(True)
        except Exception as e:
            logger.warning("Could not pin history stack width: %s", e)
        self._history_stack = history_stack
        self._history_scroll = history_scroll

        # Outer vertical stack: history (flexible) over live (fixed).
        outer_stack = NSStackView.alloc().initWithFrame_(
            NSMakeRect(16, 8, width - 32, height - 16)
        )
        outer_stack.setOrientation_(NSUserInterfaceLayoutOrientationVertical)
        outer_stack.setSpacing_(8.0)
        outer_stack.setAlignment_(_CENTER_X)
        outer_stack.setTranslatesAutoresizingMaskIntoConstraints_(True)
        outer_stack.setAutoresizingMask_(2 | 16)  # WidthSizable | HeightSizable
        outer_stack.addArrangedSubview_(history_scroll)
        outer_stack.addArrangedSubview_(live_stack)

        # Hugging priorities so history grows and live stays compact.
        # NSLayoutPriorityDefaultLow=250, DefaultHigh=750, Required=1000.
        # Orientation 1 = vertical.
        try:
            history_scroll.setContentHuggingPriority_forOrientation_(250.0, 1)
            live_stack.setContentHuggingPriority_forOrientation_(750.0, 1)
        except Exception:
            pass

        content.addSubview_(outer_stack)

        # NOW that the view hierarchy is set up (content ⊃ outer_stack
        # ⊃ live_stack ⊃ {_orig_field, _trans_field}), the widthAnchor
        # ≤ content.widthAnchor() constraints have a common ancestor
        # and can be activated without NSGenericException.
        # Verified failure 2026-05-14 log: setting these earlier (before
        # content.addSubview_) produced "no common ancestor" warning,
        # constraints silently no-op'd, and long Qwen translations
        # (188 chars in one chunk) still expanded the panel.
        try:
            for _fld in (self._orig_field, self._trans_field):
                _fld.setTranslatesAutoresizingMaskIntoConstraints_(False)
                _fld.widthAnchor().constraintLessThanOrEqualToAnchor_constant_(
                    content.widthAnchor(), -32.0
                ).setActive_(True)
            # live_stack guard layer — even if a field slipped through
            # (e.g. attributed string with no break-opportunity), the
            # stack itself can't be wider than content - 32.
            live_stack.setTranslatesAutoresizingMaskIntoConstraints_(False)
            live_stack.widthAnchor().constraintLessThanOrEqualToAnchor_constant_(
                content.widthAnchor(), -32.0
            ).setActive_(True)
            logger.info(
                "Live area widthAnchor ≤ content - 32 constraints activated"
            )
        except Exception as e:
            logger.warning("Could not pin live area max width: %s", e)

        # ── Corner buttons: ⏵/⏸ (start/stop) + ⚙ (settings) + ✕ (quit) ──
        # macOS HIG-style inline action buttons:
        #  - SF Symbols (pause.fill / play.fill / gearshape / xmark) for
        #    crisp scalable icons that look native.
        #  - HoverButton subclass: no visible bezel by default, soft
        #    translucent-white bg on cursor hover (matches Notes /
        #    Reminders inline action affordances).
        #  - Borderless so the icon is what stands out, not chrome.
        TargetCls = _make_button_target_class()
        self._button_target = TargetCls.alloc().initWithOverlay_(self)

        HoverBtnCls = _make_hover_button_class()

        # Sized down per user feedback: previous 32x32 + 16pt icon
        # made the hover bg look much bigger than the icon ("icon 和
        # 底座没对齐"). Smaller proportions keep the hover effect
        # tightly wrapped around the visible icon.
        btn_size = 26.0
        btn_gap = 2.0
        btn_y = height - btn_size - 6.0
        icon_pt = 13.0  # SF Symbol point size

        def _make_button(x, sf_name, fallback_glyph, action):
            btn = HoverBtnCls.alloc().initWithFrame_(
                NSMakeRect(x, btn_y, btn_size, btn_size)
            )
            img = _sf_symbol(sf_name, point_size=icon_pt)
            if img is not None:
                btn.setImage_(img)
                btn.setImagePosition_(2)  # NSImageOnly
                btn.setTitle_("")
            else:
                # Fallback when SF Symbols isn't available (macOS < 11).
                btn.setTitle_(fallback_glyph)
                btn.setFont_(NSFont.systemFontOfSize_(13))
            btn.setBordered_(False)
            btn.setTarget_(self._button_target)
            btn.setAction_(action)
            # Stick to top-right corner when window is resized:
            # NSViewMinXMargin (1) + NSViewMinYMargin (8) = 9.
            # Means: left margin grows, bottom margin grows, right and
            # top margins fixed → button stays glued to top-right.
            btn.setAutoresizingMask_(1 | 8)
            content.addSubview_(btn)
            return btn

        # Right-to-left: ✕, ⚙, ⏸
        self._quit_btn = _make_button(
            width - btn_size - 8.0, "xmark", "✕", b"quitClicked:",
        )
        self._settings_btn = _make_button(
            width - 2 * btn_size - 8.0 - btn_gap, "gearshape", "⚙",
            b"settingsClicked:",
        )
        self._startstop_btn = _make_button(
            width - 3 * btn_size - 8.0 - 2 * btn_gap, "pause.fill", "⏸",
            b"startStopClicked:",
        )
        self._is_running = True  # cached — flipped by set_running()

        # Audio heartbeat indicator: small colored dot in the top-LEFT
        # corner of the panel showing the audio-source state. Driven
        # by pipeline.audio_health signal (overlay.on_audio_health).
        # Colors:
        #   green  — audio chunks flowing in the last 3s
        #   gray   — pipeline running but no audio (silence / paused)
        #   red    — capture_audio subprocess died (user must restart)
        # Position: 8px from left, vertically aligned with the buttons.
        dot_size = 10.0
        dot_x = 12.0
        dot_y = btn_y + (btn_size - dot_size) / 2.0
        heartbeat = NSView.alloc().initWithFrame_(
            NSMakeRect(dot_x, dot_y, dot_size, dot_size)
        )
        heartbeat.setWantsLayer_(True)
        hb_layer = heartbeat.layer()
        hb_layer.setCornerRadius_(dot_size / 2.0)
        # Start in "idle" gray; pipeline will update via on_audio_health.
        hb_layer.setBackgroundColor_(
            NSColor.colorWithWhite_alpha_(0.55, 0.85).CGColor()
        )
        # Pin to top-left: NSViewMaxXMargin (4) + NSViewMinYMargin (8) = 12
        # means: right margin grows, bottom margin grows → stays glued
        # to top-left when window is resized.
        heartbeat.setAutoresizingMask_(4 | 8)
        content.addSubview_(heartbeat)
        self._heartbeat_layer = hb_layer

        # ── Logo (bottom-left corner) ──
        # Loads assets/logo-overlay.png from the package's `assets/`
        # directory. If the file is missing, the button still appears
        # but with no image — clicking still opens the GitHub page.
        logo_size = 22.0
        logo_x = 8.0
        logo_y = 6.0
        logo_btn = HoverBtnCls.alloc().initWithFrame_(
            NSMakeRect(logo_x, logo_y, logo_size, logo_size)
        )
        logo_btn.setBordered_(False)
        try:
            logo_btn.setButtonType_(7)  # NSMomentaryChangeButton
        except Exception:
            pass
        logo_btn.setImagePosition_(2)  # NSImageOnly
        logo_btn.setTitle_("")
        try:
            from pathlib import Path
            _logo_path = (
                Path(__file__).resolve().parent.parent
                / "assets" / "logo-overlay.png"
            )
            if _logo_path.is_file():
                from AppKit import NSImage
                _logo_img = NSImage.alloc().initWithContentsOfFile_(
                    str(_logo_path)
                )
                if _logo_img is not None:
                    _logo_img.setSize_((logo_size, logo_size))
                    logo_btn.setImage_(_logo_img)
                    logger.info("Overlay logo loaded from %s", _logo_path)
                else:
                    logger.info(
                        "Overlay logo file exists but NSImage couldn't "
                        "decode it: %s", _logo_path,
                    )
            else:
                logger.info(
                    "Overlay logo not found at %s — leaving button "
                    "image-less (still clickable to open GitHub)",
                    _logo_path,
                )
        except Exception as e:
            logger.warning("Overlay logo load failed: %s", e)
        # Pin to bottom-left.
        logo_btn.setAutoresizingMask_(4 | 32)
        logo_btn.setTarget_(self._button_target)
        logo_btn.setAction_("logoClicked:")
        try:
            logo_btn.setToolTip_("Open CaptionLM on GitHub")
        except Exception:
            pass
        content.addSubview_(logo_btn)
        self._logo_btn = logo_btn

        panel.setContentView_(content)
        # Initial wrap-width pass — fields know to wrap at this width
        # before the first subtitle arrives.
        try:
            content.applyWrapWidths()
        except Exception:
            pass
        # orderFront shows without activating — keyboard focus stays
        # in whatever app the user is using.
        panel.orderFront_(None)

        self._panel = panel
        self._content_view = content
        self._stack_view = outer_stack  # outer stack now (history scroll + live)
        logger.info(
            "NativeSubtitleOverlay: NSPanel created at (%.0f,%.0f) %dx%d, "
            "level=%d",
            x, y, int(width), int(height), int(NSScreenSaverWindowLevel),
        )

    def _make_text_field(self, font_size: int, alpha: float, bold: bool,
                         selectable: bool = False, max_lines: int = 0):
        """Create a configured NSTextField for one subtitle row.

        Properties: bezel-less, transparent bg, non-editable,
        center-aligned, word-wrapping. Font weight via bold flag.

        `selectable=True` enables mouse drag-select + Cmd+C copy. Used
        for history fields so the user can grab text from past entries
        (user feedback 2026-05-13). Live fields stay non-selectable
        because their content changes too often — a selection would
        be invalidated by the next chunk emit.
        """
        field = NSTextField.alloc().init()
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setBackgroundColor_(NSColor.clearColor())
        field.setEditable_(False)
        field.setSelectable_(bool(selectable))
        field.setStringValue_("")
        if bold:
            font = NSFont.boldSystemFontOfSize_(font_size)
        else:
            font = NSFont.systemFontOfSize_(font_size)
        field.setFont_(font)
        # Text color from settings (user-customizable). `alpha` is the
        # per-field opacity (history=0.55, orig=0.78, trans=0.98) —
        # multiplied with the configured RGB color so different rows
        # still have visual hierarchy even when the user picks a
        # non-white color.
        tc_r, tc_g, tc_b = _hex_to_rgb_floats(self.settings.overlay_text_color)
        field.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(
            tc_r, tc_g, tc_b, alpha,
        ))
        # Allow multi-line layout (default on macOS is single-line).
        # Cell wraps + line-break-by-word-wrapping for natural wrap.
        try:
            field.setUsesSingleLineMode_(False)
        except Exception:
            pass
        # Max lines: 0 = unlimited (for history fields), N>0 = truncate.
        # Live fields use max_lines=3 to prevent Qwen's occasional long
        # translations (130+ chars) from wrapping to 5-6 lines and
        # expanding the panel height (verified 2026-05-14 log line 304:
        # 132-char chunk → panel grew). Overflow handled by lineBreak-
        # ByTruncatingTail below.
        try:
            field.setMaximumNumberOfLines_(int(max_lines))
        except Exception:
            pass
        cell = field.cell()
        if cell is not None:
            cell.setWraps_(True)
            cell.setScrollable_(False)
            cell.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
            # CRITICAL ORDER: set alignment on the cell AFTER wraps /
            # lineBreakMode are configured. Empirically (2026-05-12) when
            # alignment was set on the field BEFORE cell-mods, wrapped
            # lines rendered left-aligned despite setAlignment_(Center).
            # User feedback: "实时翻译居中还是没有做成功". Setting on the
            # cell with the latest line-break config attached fixes it.
            cell.setAlignment_(_NS_TEXT_ALIGNMENT_CENTER)
        # Also set on the field (NSTextField forwards to cell, but doing
        # both keeps state coherent if cell is replaced later).
        field.setAlignment_(_NS_TEXT_ALIGNMENT_CENTER)
        return field

    # ──────────────────────────────────────────────────────────────
    # Public API (matches SubtitleOverlay)
    # ──────────────────────────────────────────────────────────────

    def update_subtitle(self, original: str, translated: str):
        """Update the live row + push chunk to history.

        New design (2026-05-13, second iteration of the day):
          - Pipeline emits each translated CHUNK separately (not
            accumulated). So each call to update_subtitle delivers
            roughly one sentence of translation.
          - Each chunk is pushed to history immediately — history
            entries are sentence-sized (user wanted ~1/3 prior length).
          - Live area accumulates chunks within an utterance for
            connected reading, BUT resets when the next utterance
            starts (signaled by _next_is_new_utterance, set by
            on_utterance_finalized() or the 2s silence timer).
        """
        new_orig = (original or "").strip()
        new_trans = (translated or "").strip()

        # FILTER 1: Punct-only chunks. Log (2026-05-13) showed pipeline
        # emitting '！'→'！', '？'→'？', '。'→'。' as their own chunks
        # when STT partial boundary fell on isolated punctuation. These
        # were each pushed to history as their own entries — the user
        # saw them as "new duplication" in the Japanese session. Skip
        # entirely (no display update, no history push) since they
        # carry zero content.
        _PUNCT_ONLY = set(".!?。！？…，、,;；:：・…．　 ")
        if new_trans and all(c in _PUNCT_ONLY for c in new_trans):
            logger.info(
                "Overlay SKIP punct-only chunk: orig=%r trans=%r",
                new_orig[:40], new_trans,
            )
            return

        # FILTER 1b: trivial-orig chunks. Log (2026-05-13 02:07 ja-JP)
        # showed entries like 'か？'→'嗎？', '？'→'嗯？', '！'→'對！'
        # polluting history. The orig is just a single char + punct
        # (e.g. 'か？' is one kana + ？). These usually represent
        # boundary slivers from STT — the meaningful content is in
        # the adjacent chunk. Skip them so they don't become their
        # own history entries.
        orig_significant = "".join(c for c in new_orig if c not in _PUNCT_ONLY)
        if len(orig_significant) <= 1:
            logger.info(
                "Overlay SKIP trivial-orig chunk: orig=%r trans=%r "
                "(orig has %d non-punct chars)",
                new_orig[:40], new_trans[:40], len(orig_significant),
            )
            return

        # FILTER 2: Pipeline boot/status messages. Log showed
        # 'Starting...'→'初始化中...' and 'Loading STT...'→'加载 语音识别...'
        # being pushed to history. These are init signals, not
        # subtitles. Update the live display so user sees boot status,
        # but DO NOT push them to history.
        _INIT_PREFIXES_EN = ("Starting", "Ready", "Loading", "Engage!")
        _INIT_PREFIXES_ZH = ("就绪", "初始化", "加载", "啟動", "就緒")
        is_init = any(new_orig.startswith(p) for p in _INIT_PREFIXES_EN) or \
                  any(new_trans.startswith(p) for p in _INIT_PREFIXES_ZH)
        if is_init:
            logger.info(
                "Overlay init message (no history push): %r → %r",
                new_orig[:40], new_trans[:40],
            )
            # Update live display for visibility, then return
            self._current_orig = new_orig
            self._current_trans = new_trans
            self._next_is_new_utterance = True  # don't accumulate after init
            if self._orig_field is not None:
                _set_centered_text(self._orig_field, self._current_orig)
            if self._trans_field is not None:
                _set_centered_text(self._trans_field, self._current_trans)
            return

        # Sentence-based push (2026-05-13, redesigned again).
        # PRIOR approach: pushed every chunk → 30+ history entries per
        # minute of speech, many being progressive variants when STT
        # revised partials. User screenshot showed mixed long/short
        # entries with duplication.
        # NEW approach: don't push per chunk; accumulate into live;
        # push to history ONLY when the accumulated trans ends with
        # sentence-end punctuation (or utterance_finalized fires).
        # That keeps history entries sentence-sized (~ what user wants)
        # AND drastically reduces opportunities for progressive
        # snapshots since we wait until a sentence is fully baked.

        # 2. Update the live-area accumulator with defensive dedup.
        #    Even if pipeline emits a duplicate chunk (utterance reset
        #    edge case), the overlay should NOT append duplicated text
        #    to the live display. Decision tree on the NEW chunk vs
        #    the current accumulator:
        branch = "unknown"
        if self._next_is_new_utterance or not self._current_orig:
            self._current_orig = new_orig
            self._current_trans = new_trans
            self._next_is_new_utterance = False
            branch = "FRESH"
        elif new_trans and new_trans in self._current_trans:
            branch = "SKIP-already-in-current"
        elif new_trans and self._current_trans.endswith(new_trans):
            branch = "SKIP-tail-match"
        elif new_trans and new_trans.startswith(self._current_trans):
            self._current_orig = new_orig
            self._current_trans = new_trans
            branch = "REPLACE-with-longer"
        else:
            sep_orig = " " if (self._current_orig and new_orig) else ""
            sep_trans = " " if (self._current_trans and new_trans) else ""
            self._current_orig = (self._current_orig + sep_orig + new_orig).strip()
            self._current_trans = (self._current_trans + sep_trans + new_trans).strip()
            branch = "APPEND"

        # 2b. Sentence-end commit: when the accumulated trans ends with
        # sentence-end punctuation, the live area holds a COMPLETE
        # sentence — push that as ONE history entry and reset live for
        # the next sentence. (Chinese STT rarely emits is_final, so we
        # can't rely on utterance_finalized to mark sentence boundaries.)
        #
        # SKIPPED in streaming-translator mode (Qwen): the engine emits
        # is_final at the right boundary, and Qwen partials carry
        # sentence-end punct on EVERY update, so the heuristic produces
        # fragmented history. Trust on_utterance_finalized only.
        if not self._streaming_translator_mode:
            _SENTENCE_END = (".", "!", "?", "。", "！", "？", "…")
            trans_stripped_for_end_check = self._current_trans.rstrip()
            if trans_stripped_for_end_check.endswith(_SENTENCE_END):
                if self._current_orig and self._current_trans:
                    self._push_pair_to_history(
                        self._current_orig, self._current_trans,
                    )
                self._next_is_new_utterance = True
                branch += "+sentence-commit"

        logger.info(
            "Overlay update_subtitle: chunk=%r → live[%d chars] (branch=%s)",
            new_trans[:60], len(self._current_trans), branch,
        )

        # 3. Restart the 2s silence timer so the next utterance boundary
        #    is detected if STT never emits is_final.
        #    In streaming-translator mode, the engine's is_final is
        #    authoritative — no silence-timer fallback (it would push
        #    duplicates of what utterance_finalized already pushed).
        if not self._streaming_translator_mode:
            self._commit_timer.stop()
            self._commit_timer.start()

        # 4. Render the accumulated live content.
        if self._orig_field is not None:
            _set_centered_text(self._orig_field, self._current_orig)
        if self._trans_field is not None:
            _set_centered_text(self._trans_field, self._current_trans)

    def on_translation_updated(self, orig: str, new_trans: str):
        """Pipeline re-translated a previously-committed utterance with
        future context. Find the matching entry in our committed
        history (keyed by orig text) and update its translation in
        place. The history label refreshes; the user sees the polished
        translation replace the older one.

        If the matching utterance is still the LIVE row (hasn't been
        pushed to history yet), update there instead.
        """
        # First check live row.
        if self._current_orig == orig and self._current_trans != new_trans:
            self._current_trans = new_trans
            if self._trans_field is not None:
                _set_centered_text(self._trans_field, new_trans)
            return
        # Then walk committed history.
        for i, (o, t) in enumerate(self._committed_pairs):
            if o == orig and t != new_trans:
                self._committed_pairs[i] = (o, new_trans)
                self._refresh_history_fields()
                return

    def set_streaming_translator_mode(self, enabled: bool):
        """Enable/disable streaming-translator mode.

        When enabled (Qwen LiveTranslate path), the overlay trusts
        on_utterance_finalized as the sole commit signal — sentence-end
        punct heuristic and 2s silence-timer fallback are both disabled.
        This avoids duplicate/fragmented history entries when the engine
        sends cumulative partials with terminal punctuation on every
        update.

        Called from app.py at pipeline start when the connected STT has
        provides_translation=True.
        """
        if enabled == self._streaming_translator_mode:
            return
        self._streaming_translator_mode = enabled
        # Stop any pending silence-timer commit when switching INTO
        # streaming mode — that fallback isn't wanted here.
        if enabled:
            try:
                self._commit_timer.stop()
            except Exception:
                pass
        logger.info(
            "Overlay streaming-translator mode = %s "
            "(sentence-commit + silence-timer %s)",
            enabled, "DISABLED" if enabled else "ENABLED",
        )

    def on_utterance_finalized(self):
        """STT signaled is_final — push whatever's accumulated in the
        live area to history (even if it doesn't end with sentence
        punct, since this is a true utterance boundary), then mark
        the live-area accumulator to start fresh on the next chunk."""
        logger.info(
            "Overlay on_utterance_finalized: pushing pending live "
            "(%d chars) and marking next-is-new",
            len(self._current_trans),
        )
        if self._current_orig and self._current_trans:
            self._push_pair_to_history(self._current_orig, self._current_trans)
        self._next_is_new_utterance = True

    def show_rate_limit_warning(self, provider: str):
        """Display a temporary warning in the original-label row."""
        if self._orig_field is not None:
            _set_centered_text(self._orig_field, f"⚠ {provider} rate limit")
        # Clear after 10s.
        QTimer.singleShot(10_000, lambda: self._clear_warning())

    def _clear_warning(self):
        if self._orig_field is not None:
            _set_centered_text(self._orig_field, self._current_orig)

    def clear_history(self):
        """Reset overlay state for a new session."""
        self._committed_pairs.clear()
        self._current_orig = ""
        self._current_trans = ""
        self._next_is_new_utterance = True
        for field in self._history_fields:
            field.setStringValue_("")
            field.setHidden_(True)
        if self._orig_field is not None:
            self._orig_field.setStringValue_("")
        if self._trans_field is not None:
            self._trans_field.setStringValue_("")

    def apply_settings(self, settings: Settings):
        """Re-apply visual settings. Triggered when the user changes
        anything in the Control Panel that affects the overlay.

        Updates:
          - fonts: live trans (bold @ translated_font_size), live orig
            (regular @ original_font_size), history (regular @ trans-2)
          - text color (settings.overlay_text_color) applied to all fields
          - bg color + opacity (settings.overlay_bg_color, overlay_opacity)
            applied to the content layer
        """
        self.settings = settings
        # Font sizes
        if self._trans_field is not None:
            self._trans_field.setFont_(
                NSFont.boldSystemFontOfSize_(settings.translated_font_size)
            )
        if self._orig_field is not None:
            self._orig_field.setFont_(
                NSFont.systemFontOfSize_(settings.original_font_size)
            )
        history_size = max(8, int(settings.translated_font_size) - 2)
        for field in self._history_fields:
            field.setFont_(NSFont.systemFontOfSize_(history_size))

        # Text color — each field keeps its alpha tier (history 0.55,
        # orig 0.78, trans 0.98) so visual hierarchy is preserved
        # regardless of the user's chosen color.
        try:
            tc_r, tc_g, tc_b = _hex_to_rgb_floats(settings.overlay_text_color)
            if self._trans_field is not None:
                self._trans_field.setTextColor_(
                    NSColor.colorWithRed_green_blue_alpha_(tc_r, tc_g, tc_b, 0.98)
                )
            if self._orig_field is not None:
                self._orig_field.setTextColor_(
                    NSColor.colorWithRed_green_blue_alpha_(tc_r, tc_g, tc_b, 0.78)
                )
            for field in self._history_fields:
                field.setTextColor_(
                    NSColor.colorWithRed_green_blue_alpha_(tc_r, tc_g, tc_b, 0.55)
                )
        except Exception as e:
            logger.warning("Could not apply text color: %s", e)

        # Background color + opacity
        try:
            layer = getattr(self, "_content_layer", None)
            if layer is not None:
                bg_r, bg_g, bg_b = _hex_to_rgb_floats(settings.overlay_bg_color)
                bg_alpha = max(0.0, min(1.0, settings.overlay_opacity / 100.0))
                new_bg = NSColor.colorWithRed_green_blue_alpha_(
                    bg_r, bg_g, bg_b, bg_alpha,
                )
                layer.setBackgroundColor_(new_bg.CGColor())
        except Exception as e:
            logger.warning("Could not apply bg color/opacity: %s", e)

        # Re-render with new text color since _set_centered_text builds
        # the attributed string using the field's current textColor.
        if self._orig_field is not None and self._current_orig:
            _set_centered_text(self._orig_field, self._current_orig)
        if self._trans_field is not None and self._current_trans:
            _set_centered_text(self._trans_field, self._current_trans)
        self._refresh_history_fields()

    def show(self):
        if self._panel is not None:
            self._panel.orderFront_(None)
            self._visible = True

    def hide(self):
        if self._panel is not None:
            self._panel.orderOut_(None)
            self._visible = False

    def toggle_visibility(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    def on_audio_health(self, state: str):
        """Update the heartbeat dot color based on audio source state.
        Called from pipeline.audio_health signal (~ 1 Hz).

        state: 'alive' (green) | 'idle' (gray) | 'dead' (red)
        """
        # Log state TRANSITIONS only (not every tick) so the file log
        # stays useful for grep without 30+ duplicate entries/min.
        last = getattr(self, "_last_audio_health", None)
        if state != last:
            logger.info("Overlay heartbeat: %s → %s", last, state)
            self._last_audio_health = state
        layer = getattr(self, "_heartbeat_layer", None)
        if layer is None:
            return
        try:
            if state == "alive":
                # Apple HIG system green-ish
                c = NSColor.colorWithRed_green_blue_alpha_(0.20, 0.78, 0.35, 0.95)
            elif state == "dead":
                # Apple HIG system red
                c = NSColor.colorWithRed_green_blue_alpha_(1.0, 0.27, 0.23, 0.95)
            else:
                # Idle gray
                c = NSColor.colorWithWhite_alpha_(0.55, 0.85)
            layer.setBackgroundColor_(c.CGColor())
        except Exception as e:
            logger.debug("on_audio_health update failed: %s", e)

    def set_running(self, running: bool):
        """Sync the start/stop button icon with pipeline state.
        pause.fill when running (click to stop), play.fill when stopped."""
        self._is_running = running
        if hasattr(self, "_startstop_btn") and self._startstop_btn is not None:
            sf_name = "pause.fill" if running else "play.fill"
            img = _sf_symbol(sf_name, point_size=13.0)
            if img is not None:
                self._startstop_btn.setImage_(img)
                self._startstop_btn.setTitle_("")
            else:
                self._startstop_btn.setTitle_("⏸" if running else "⏵")

    # ──────────────────────────────────────────────────────────────
    # History commit + dedup (ported from Qt overlay)
    # ──────────────────────────────────────────────────────────────

    def _commit_current_to_history(self):
        """2-second silence fallback — push pending live to history
        (in case the dangling sentence has no terminator), and mark
        the next chunk as starting fresh."""
        logger.info(
            "Overlay silence-timer fired (2s no chunks) — pushing "
            "pending live (%d chars) and marking next-is-new",
            len(self._current_trans),
        )
        if self._current_orig and self._current_trans:
            self._push_pair_to_history(self._current_orig, self._current_trans)
        self._next_is_new_utterance = True

    def _push_pair_to_history(self, orig: str, trans: str):
        """Add a single (orig, trans) pair to the history list with
        dedup. Idempotent — calling with the same pair twice in a row
        is a no-op (already at top). Doesn't touch the live row.

        Dedup heuristic mirrors SessionRecorder collapse logic so the
        on-screen history matches the SRT export view: if the new
        original is a refinement of the last (prefix-extending or
        substring), REPLACE rather than append.
        """
        if not orig:
            return
        pair = (orig, trans)
        if self._committed_pairs:
            last_orig, last_trans = self._committed_pairs[-1]
            if pair == (last_orig, last_trans):
                logger.info(
                    "History push SKIP-exact-dup: %r → %r",
                    orig[:40], trans[:40],
                )
                return  # exact dup; already at top

            stripped_last = last_orig.rstrip(_TRAILING_PUNCT)
            stripped_new = orig.rstrip(_TRAILING_PUNCT)
            # ALSO compare with whitespace removed — STT sometimes
            # inserts/removes a space mid-utterance (verified
            # 2026-05-13 ja-JP log: 'カメラ見えますか？' vs
            # 'カメラ 見えますか？' — same content, fails naive
            # substring check because of the space). Without this,
            # progressive accumulations APPEND when they should
            # REPLACE.
            def _nospace(s):
                return "".join(c for c in s if not c.isspace())
            ns_last = _nospace(stripped_last)
            ns_new = _nospace(stripped_new)
            if (
                stripped_last
                and stripped_new
                and (
                    orig.startswith(stripped_last)
                    or last_orig.startswith(stripped_new)
                    or stripped_last in orig
                    or stripped_new in last_orig
                    # whitespace-normalized variants
                    or ns_new.startswith(ns_last)
                    or ns_last.startswith(ns_new)
                    or ns_last in ns_new
                    or ns_new in ns_last
                )
            ):
                if len(orig) >= len(last_orig):
                    self._committed_pairs[-1] = pair
                    logger.info(
                        "History push REPLACE (prefix-extending): "
                        "%r → %r (was %r)",
                        orig[:40], trans[:40], last_orig[:40],
                    )
                else:
                    logger.info(
                        "History push SKIP-prefix-shorter: "
                        "%r → %r (last was longer %r)",
                        orig[:40], trans[:40], last_orig[:40],
                    )
                self._log_history_state()
                self._refresh_history_fields()
                return

        self._committed_pairs.append(pair)
        if len(self._committed_pairs) > self._MAX_COMMITTED:
            self._committed_pairs = self._committed_pairs[-self._MAX_COMMITTED:]
        logger.info(
            "History push APPEND (total=%d): %r → %r",
            len(self._committed_pairs), orig[:40], trans[:40],
        )
        self._log_history_state()
        self._refresh_history_fields()

    def _log_history_state(self) -> None:
        """Dump the LAST N committed history pairs to the log so we
        can see what the user actually sees in the history area when
        troubleshooting. Each pair shown truncated to ~60 chars.
        User feedback (2026-05-13): "你的log里面应该也显示一下最后
        我会看到的稳定历史区, 这样你就能更好的troubleshoot".
        """
        N = 6
        recent = self._committed_pairs[-N:]
        logger.info(
            "History state (last %d of %d total):",
            len(recent), len(self._committed_pairs),
        )
        for i, (o, t) in enumerate(recent):
            idx = len(self._committed_pairs) - len(recent) + i + 1
            logger.info(
                "  [%d] orig=%r trans=%r",
                idx, o[:60], t[:60],
            )

    def _content_overlaps(self, a: str, b: str) -> bool:
        """True if the two originals look like the same utterance
        (one extends or contains the other). Used by update_subtitle
        to decide whether a new subtitle is a continuation of the
        current one or the start of a new utterance."""
        if not a or not b:
            return False
        a_stripped = a.rstrip(_TRAILING_PUNCT)
        b_stripped = b.rstrip(_TRAILING_PUNCT)
        if not a_stripped or not b_stripped:
            return False
        if len(a_stripped) < 2 or len(b_stripped) < 2:
            return True
        return (
            a.startswith(b_stripped)
            or b.startswith(a_stripped)
            or a_stripped in b
            or b_stripped in a
        )

    def _refresh_history_fields(self):
        # 1. Sample scroll state BEFORE we mutate fields so we know
        #    whether the user is currently looking at the newest row.
        #    User feedback (2026-05-13): "每次更新历史的时候，历史都
        #    会强行跳回最上面". Previously we auto-scrolled to the
        #    very top of the doc on every refresh, which destroyed
        #    the user's position whenever they scrolled up to read
        #    older entries.
        scroll = getattr(self, "_history_scroll", None)
        stack = getattr(self, "_history_stack", None)
        was_at_newest = True  # default: first refresh, treat as "at newest"
        if scroll is not None:
            try:
                clip = scroll.contentView()
                doc_view = scroll.documentView()
                if clip is not None and doc_view is not None:
                    visible = clip.bounds()
                    doc_height = float(doc_view.frame().size.height)
                    if bool(doc_view.isFlipped()):
                        # Flipped: y=0 at top, newest (bottom of stack)
                        # at y=doc_height. User "at newest" means
                        # visible bottom edge is close to doc_height.
                        v_bottom = float(visible.origin.y) + float(visible.size.height)
                        was_at_newest = v_bottom >= (doc_height - 8.0)
                    else:
                        # Non-flipped: y=0 at bottom (newest), y=height
                        # at top (oldest). User "at newest" means
                        # visible.origin.y is close to 0.
                        was_at_newest = float(visible.origin.y) <= 8.0
            except Exception:
                was_at_newest = True

        # 2. Populate fields with the latest committed pairs.
        recent = self._committed_pairs[-self._HISTORY_ROWS:]
        padded = [("", "")] * (self._HISTORY_ROWS - len(recent)) + list(recent)
        last_visible_field = None
        for field, (_, trans) in zip(self._history_fields, padded):
            text = trans or ""
            _set_centered_text(field, text)
            is_visible = bool(text.strip())
            # Hide empty rows so NSStackView collapses them — keeps
            # the panel visually tight when history isn't fully populated.
            field.setHidden_(not is_visible)
            if is_visible:
                last_visible_field = field

        # 3. Only auto-scroll if user was already at newest. Otherwise
        #    leave their scroll position alone so they can read older
        #    entries undisturbed. Using scrollRectToVisible on the last
        #    visible field handles flipped/non-flipped coords correctly.
        if was_at_newest and last_visible_field is not None and stack is not None:
            try:
                stack.layoutSubtreeIfNeeded()
                last_visible_field.scrollRectToVisible_(
                    last_visible_field.bounds()
                )
            except Exception:
                pass
