/*
 * A small D-Bus service exposing the things gnome-shell knows and refuses to
 * tell an ordinary client on Wayland.
 *
 * Everything here was tried the documented way first and denied:
 *
 *   org.gnome.Shell.Screenshot.Screenshot  -> AccessDenied: Screenshot is not allowed
 *   org.gnome.Shell.GrabAccelerator        -> AccessDenied: GrabAccelerator is not allowed
 *
 * and window geometry / focus control has no client-side API at all, because a
 * Wayland client is not allowed to know where it is or to raise itself. Inside
 * the compositor all of these are ordinary calls.
 *
 * Interface: com.zeticle.deskwright at /com/zeticle/deskwright
 */

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {ActivityIndicator} from './indicator.js';

/* Bumped by hand whenever this file changes, so a client can tell a running
 * shell that predates the change from one that has it. See Ping. */
const BUILD = '2026-08-23.1';

/* Halt banner. Kept in one place so the keybinding hint in it can never drift
 * from the schema default without someone noticing this constant. */
const HALT_LABEL = 'HALTED — <Super><Ctrl>Esc to clear';

/* How long each halt border lease lasts and how often it is renewed. The
 * indicator deliberately always carries a deadline (a stuck border trains you
 * to ignore it), so a persistent HALTED state is expressed as a short lease
 * renewed on a timer for as long as the halt is engaged. */
const HALT_BORDER_TTL_SECONDS = 90;
const HALT_BORDER_REFRESH_SECONDS = 60;

/* The keybinding-toggle debounce, in microseconds (monotonic clock). See
 * toggleHaltFromKeybinding for the honest limits of this. */
const HALT_DEBOUNCE_US = 2 * 1000 * 1000;

const IFACE = `
<node>
  <interface name="com.zeticle.deskwright">
    <method name="Screenshot">
      <arg type="s" direction="in"  name="path"/>
      <arg type="b" direction="in"  name="includeCursor"/>
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="detail"/>
    </method>
    <method name="Ping">
      <arg type="s" direction="out" name="build"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="Pointer">
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="WindowAt">
      <arg type="i" direction="in"  name="x"/>
      <arg type="i" direction="in"  name="y"/>
      <arg type="s" direction="out" name="json"/>
    </method>
    <method name="ScreenshotWindow">
      <arg type="u" direction="in"  name="id"/>
      <arg type="s" direction="in"  name="path"/>
      <arg type="b" direction="in"  name="includeFrame"/>
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="detail"/>
    </method>
    <method name="ScreenshotArea">
      <arg type="i" direction="in"  name="x"/>
      <arg type="i" direction="in"  name="y"/>
      <arg type="i" direction="in"  name="width"/>
      <arg type="i" direction="in"  name="height"/>
      <arg type="s" direction="in"  name="path"/>
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="detail"/>
    </method>
    <method name="ActivateWindow">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="MoveResize">
      <arg type="u" direction="in"  name="id"/>
      <arg type="i" direction="in"  name="x"/>
      <arg type="i" direction="in"  name="y"/>
      <arg type="i" direction="in"  name="width"/>
      <arg type="i" direction="in"  name="height"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="Close">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="Minimize">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="Unminimize">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="Maximize">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="in"  name="maximize"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="SetWorkspace">
      <arg type="u" direction="in"  name="id"/>
      <arg type="i" direction="in"  name="index"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="SetAbove">
      <arg type="u" direction="in"  name="id"/>
      <arg type="b" direction="in"  name="above"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="SetIndicator">
      <arg type="s" direction="in"  name="label"/>
      <arg type="d" direction="in"  name="ttlSeconds"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="ClearIndicator">
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="IndicatorActive">
      <arg type="b" direction="out" name="active"/>
    </method>
    <method name="SetClipboardText">
      <arg type="s" direction="in"  name="text"/>
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="detail"/>
    </method>
    <method name="SetClipboardFile">
      <arg type="s" direction="in"  name="mimetype"/>
      <arg type="s" direction="in"  name="path"/>
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="detail"/>
    </method>
    <method name="GetClipboardText">
      <arg type="b" direction="out" name="success"/>
      <arg type="s" direction="out" name="text"/>
    </method>
    <method name="Halt">
    </method>
    <method name="ClearHalt">
    </method>
    <method name="HaltActive">
      <arg type="b" direction="out" name="active"/>
    </method>
  </interface>
</node>`;

export class DBusService {
    enable() {
        this._indicator = new ActivityIndicator();
        this._halted = false;
        this._haltEngagedAtUs = 0;
        this._haltRefreshId = 0;
        this._impl = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._impl.export(Gio.DBus.session, '/com/zeticle/deskwright');
        this._nameId = Gio.bus_own_name(
            Gio.BusType.SESSION,
            'com.zeticle.deskwright',
            Gio.BusNameOwnerFlags.NONE,
            null,
            null,
            null
        );
    }

    disable() {
        if (this._haltRefreshId) {
            GLib.source_remove(this._haltRefreshId);
            this._haltRefreshId = 0;
        }
        this._halted = false;
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = null;
        }
        if (this._impl) {
            this._impl.unexport();
            this._impl = null;
        }
    }

    // Async because the capture is: gjs looks for the `Async` suffix and hands
    // us the invocation to answer whenever we are ready.
    ScreenshotAsync([path, includeCursor], invocation) {
        const done = (ok, detail) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, detail]));

        let stream;
        try {
            stream = Gio.File.new_for_path(path).replace(
                null,
                false,
                Gio.FileCreateFlags.NONE,
                null
            );
        } catch (e) {
            done(false, `cannot write ${path}: ${e.message}`);
            return;
        }

        const shooter = new Shell.Screenshot();
        shooter.screenshot(includeCursor, stream, (obj, res) => {
            try {
                shooter.screenshot_finish(res);
                stream.close(null);
                done(true, path);
            } catch (e) {
                done(false, e.message);
            }
        });
    }

    /* Put something on the clipboard the way gnome-shell itself does.
     *
     * Why this is not just wl-copy: clipboard-indicator asks Mutter for
     * "text/plain;charset=utf-8" BEFORE it asks for any image type, and when
     * the offer came from wl-copy, Mutter answers that request with the image
     * bytes. The result is a history entry whose mimetype says text and whose
     * contents are a PNG. Reproduced three times with fresh content on
     * 2026-08-22; meanwhile a plain Wayland client asking the same question
     * (wl-paste --type text/plain) correctly gets nothing. GNOME's own
     * screenshot is unaffected because it sets the clipboard from in here.
     * So do the same. See FINDINGS S-018.
     */
    SetClipboardTextAsync([text], invocation) {
        const done = (ok, detail) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, detail]));
        try {
            St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
            done(true, `${text.length} chars`);
        } catch (e) {
            done(false, e.message);
        }
    }

    /* The file is read here rather than shipped over D-Bus: a full-screen PNG
     * is megabytes, and the bus is the wrong place for that. */
    SetClipboardFileAsync([mimetype, path], invocation) {
        const done = (ok, detail) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, detail]));
        try {
            const [ok, bytes] = GLib.file_get_contents(path);
            if (!ok || !bytes || bytes.length === 0) {
                done(false, `read nothing from ${path}`);
                return;
            }
            St.Clipboard.get_default().set_content(
                St.ClipboardType.CLIPBOARD,
                mimetype,
                new GLib.Bytes(bytes)
            );
            done(true, `${bytes.length} bytes as ${mimetype}`);
        } catch (e) {
            done(false, e.message);
        }
    }

    /* Read the clipboard the same way it is written above: through St, which is
     * gnome-shell's own view of the selection, so it agrees with what a paste
     * would produce. get_text is callback-style, hence the Async wrapper.
     * success=false means "no text available", which is a normal answer (the
     * clipboard may hold an image or nothing), not an error. */
    GetClipboardTextAsync(params, invocation) {
        try {
            St.Clipboard.get_default().get_text(
                St.ClipboardType.CLIPBOARD,
                (clipboard, text) => {
                    invocation.return_value(new GLib.Variant('(bs)',
                        [text !== null && text !== undefined, text ?? '']));
                }
            );
        } catch (e) {
            invocation.return_value(new GLib.Variant('(bs)', [false, '']));
        }
    }

    /* What build of this file is actually loaded.
     *
     * gnome-shell imports an extension's modules once per session and there is
     * no way to reload them on Wayland (ReloadExtension answers "deprecated and
     * does not work" on 50.1), so an edited file and a running shell can differ
     * for days. A client that needs Pointer/WindowAt asks for this first and
     * can then say "log out and back in" instead of failing with UnknownMethod.
     */
    Ping() {
        return JSON.stringify({
            build: BUILD,
            methods: ['Screenshot', 'ScreenshotArea', 'ScreenshotWindow',
                      'ListWindows', 'Pointer', 'WindowAt', 'ActivateWindow',
                      'MoveResize', 'Close', 'Minimize', 'Unminimize',
                      'Maximize', 'SetWorkspace', 'SetAbove',
                      'SetIndicator', 'ClearIndicator', 'IndicatorActive',
                      'SetClipboardText', 'SetClipboardFile', 'GetClipboardText',
                      'Halt', 'ClearHalt', 'HaltActive'],
        });
    }

    /* Where the pointer is, in the same global logical coordinates ListWindows
     * reports geometry in.
     *
     * No Wayland client may ask this, and asking X through XWayland returns the
     * last position the pointer had over an X surface, which is stale and
     * silently wrong -- measured as a constant (960, 540) for an entire session
     * on 2026-08-08. Inside the compositor it is one call.
     */
    Pointer() {
        const [x, y, mods] = global.get_pointer();
        return JSON.stringify({x, y, mods});
    }

    /* Which window a click at (x, y) would actually be delivered to.
     *
     * Deliberately picked from the scene graph rather than compared against
     * window rectangles: an overlay with an input shape (pipsqueak's cards, a
     * click-through HUD) covers the rectangle without accepting the click, and
     * the pointer lands on whatever is underneath. Picking asks the same
     * question the compositor asks when routing a real click, so it agrees with
     * it. `covering` lists the windows whose rectangle contains the point,
     * top first, so a caller can see the difference.
     */
    WindowAt(x, y) {
        const describe = win => {
            const r = win.get_frame_rect();
            return {
                id: win.get_id(),
                pid: win.get_pid(),
                wm_class: win.get_wm_class(),
                title: win.get_title(),
                x: r.x, y: r.y, width: r.width, height: r.height,
                focused: win.has_focus(),
            };
        };

        let hit = null;
        try {
            let actor = global.stage.get_actor_at_pos(Clutter.PickMode.REACTIVE, x, y);
            while (actor && !(actor instanceof Meta.WindowActor))
                actor = actor.get_parent();
            if (actor?.meta_window)
                hit = describe(actor.meta_window);
        } catch (e) {
            hit = null;
        }

        const covering = [];
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (win.minimized)
                continue;
            const r = win.get_frame_rect();
            if (x >= r.x && y >= r.y && x < r.x + r.width && y < r.y + r.height)
                covering.unshift(describe(win));   // get_window_actors is bottom-first
        }
        return JSON.stringify({x, y, window: hit, covering});
    }

    ListWindows() {
        const windows = global.get_window_actors().map(actor => {
            const win = actor.meta_window;
            const rect = win.get_frame_rect();
            return {
                id: win.get_id(),
                // Two windows of the same application differ only by title,
                // and a title changes. Telling a dev build from the installed
                // one took a readlink loop over /proc per window; the
                // compositor has known all along.
                pid: win.get_pid(),
                wm_class: win.get_wm_class(),
                title: win.get_title(),
                x: rect.x,
                y: rect.y,
                width: rect.width,
                height: rect.height,
                focused: win.has_focus(),
                minimized: win.minimized,
                above: win.above,
                workspace: win.get_workspace()?.index() ?? -1,
                type: Object.keys(Meta.WindowType).find(
                    k => Meta.WindowType[k] === win.window_type
                ),
            };
        });
        return JSON.stringify(windows, null, 2);
    }

    /* Capture a rectangle instead of the whole screen.
     *
     * The client could crop a full capture, and did: every look at one window
     * cost a 2 MB PNG and an ImageMagick call. Cropping in the compositor is
     * one call and returns only the pixels asked for.
     */
    ScreenshotAreaAsync([x, y, width, height, path], invocation) {
        this._captureArea(x, y, width, height, path, invocation);
    }

    /* The same, for one window by id, whether or not it is focused.
     *
     * Shell.Screenshot.screenshot_window only ever captures the *focused*
     * window, which is no use to a client watching something in the background,
     * so this resolves the id to a rectangle and captures that. It therefore
     * captures what is ON SCREEN there: a window with something in front of it
     * comes back with the thing in front of it, which is the honest answer and
     * why the reply says so.
     */
    ScreenshotWindowAsync([id, path, includeFrame], invocation) {
        const done = (ok, detail) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, detail]));

        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (win.get_id() !== id)
                continue;
            if (win.minimized) {
                done(false, `window ${id} is minimized, so there is nothing on screen to capture`);
                return;
            }
            const r = includeFrame ? win.get_buffer_rect() : win.get_frame_rect();
            this._captureArea(r.x, r.y, r.width, r.height, path, invocation);
            return;
        }
        done(false, `no window with id ${id}`);
    }

    _captureArea(x, y, width, height, path, invocation) {
        const done = (ok, detail) =>
            invocation.return_value(new GLib.Variant('(bs)', [ok, detail]));

        if (width <= 0 || height <= 0) {
            done(false, `refusing to capture a ${width}x${height} area`);
            return;
        }

        let stream;
        try {
            stream = Gio.File.new_for_path(path).replace(
                null, false, Gio.FileCreateFlags.NONE, null);
        } catch (e) {
            done(false, `cannot write ${path}: ${e.message}`);
            return;
        }

        const shooter = new Shell.Screenshot();
        shooter.screenshot_area(x, y, width, height, stream, (obj, res) => {
            try {
                shooter.screenshot_area_finish(res);
                stream.close(null);
                done(true, `${path} ${width}x${height}+${x}+${y}`);
            } catch (e) {
                done(false, e.message);
            }
        });
    }

    ActivateWindow(id) {
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (win.get_id() !== id)
                continue;
            // A timestamp of 0 makes mutter treat this as a user action rather
            // than a stale request it is entitled to ignore.
            win.activate(global.get_current_time());
            return true;
        }
        return false;
    }

    // --------------------------------------------------------- window control

    /* Every verb below shares the same contract: resolve the id, do the thing,
     * return true; unknown id or any Meta refusal returns false. Nothing here
     * may throw across the bus -- a compositor-side exception surfaces to the
     * client as a cryptic GDBus error and, worse, lands in the shell's log as
     * if the shell itself misbehaved. */

    _findWindow(id) {
        for (const actor of global.get_window_actors()) {
            const win = actor.meta_window;
            if (win.get_id() === id)
                return win;
        }
        return null;
    }

    /* user_op=true tells mutter this is a user-initiated move/resize, so it is
     * honored rather than treated as an app repositioning itself (which mutter
     * is entitled to ignore or clamp). Mutter still enforces the window's own
     * constraints -- minimum sizes, non-resizable windows -- so the resulting
     * geometry can differ from the request; read it back with ListWindows. */
    MoveResize(id, x, y, width, height) {
        const win = this._findWindow(id);
        if (!win || width <= 0 || height <= 0)
            return false;
        try {
            win.move_resize_frame(true, x, y, width, height);
            return true;
        } catch (e) {
            return false;
        }
    }

    /* delete() is the polite close -- the app gets WM_DELETE_WINDOW / xdg
     * close and may prompt to save. The timestamp marks it a user action. */
    Close(id) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            win.delete(global.get_current_time());
            return true;
        } catch (e) {
            return false;
        }
    }

    Minimize(id) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            win.minimize();
            return true;
        } catch (e) {
            return false;
        }
    }

    Unminimize(id) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            win.unminimize();
            return true;
        } catch (e) {
            return false;
        }
    }

    /* maximize=true maximizes both directions, false restores. Mutter dropped
     * directional (H-only/V-only) maximization and with it the flags argument
     * in newer releases — but Meta.MaximizeFlags itself is still exported on
     * GNOME 50, so probing the enum picks the WRONG arm there ("Too many
     * arguments to method Meta.Window.maximize"). Probe the call, not the
     * enum: try the no-argument form first and fall back to the flags form on
     * older shells. */
    Maximize(id, maximize) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            try {
                if (maximize)
                    win.maximize();
                else
                    win.unmaximize();
            } catch (argError) {
                const flags = Meta.MaximizeFlags?.BOTH;
                if (flags === undefined)
                    throw argError;
                if (maximize)
                    win.maximize(flags);
                else
                    win.unmaximize(flags);
            }
            return true;
        } catch (e) {
            return false;
        }
    }

    /* append=false: an out-of-range index is refused here rather than silently
     * creating workspaces. */
    SetWorkspace(id, index) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            if (index < 0 || index >= global.workspace_manager.get_n_workspaces())
                return false;
            win.change_workspace_by_index(index, false);
            return true;
        } catch (e) {
            return false;
        }
    }

    SetAbove(id, above) {
        const win = this._findWindow(id);
        if (!win)
            return false;
        try {
            if (above)
                win.make_above();
            else
                win.unmake_above();
            return true;
        } catch (e) {
            return false;
        }
    }

    // -------------------------------------------------------------- kill switch

    /* Halt is a human override: a flag an agent-side client is expected to poll
     * (HaltActive) and stop on. The extension itself keeps answering -- reads
     * like ListWindows are how a stopped agent explains itself -- enforcement
     * belongs to the client, the flag belongs to the human.
     *
     * The state is deliberately session-scoped and in-memory only: a reboot or
     * re-login clears it, nothing persists it, and nothing but the keybinding
     * or an explicit ClearHalt call clears it while the session lives.
     *
     * HONEST LIMITATION: injected input can, in principle, press the same
     * keybinding combo a human would. The 2-second debounce below defeats the
     * cheapest attack -- an agent blindly double-pressing the combo to engage
     * and immediately clear -- but an agent that deliberately waits 2 seconds
     * and presses again CAN clear its own halt. A compositor cannot tell
     * ydotool's uinput events from a keyboard's; a stronger guarantee would
     * need evdev-level provenance, which does not exist. The debounce raises
     * the bar from "accident" to "deliberate", which is what it can honestly
     * buy. */

    toggleHaltFromKeybinding() {
        const now = GLib.get_monotonic_time();
        if (!this._halted) {
            this._engageHalt(now);
            return;
        }
        // Second press within the debounce window of the halt does NOT clear
        // it (see the limitation note above).
        if (now - this._haltEngagedAtUs < HALT_DEBOUNCE_US)
            return;
        this._clearHalt();
    }

    _engageHalt(nowUs) {
        this._halted = true;
        // The timestamp is set on EVERY engage, including over D-Bus, so a
        // D-Bus Halt followed by an instantly injected keypress is debounced
        // exactly like a double-press.
        this._haltEngagedAtUs = nowUs ?? GLib.get_monotonic_time();
        this._showHaltBorder();
        if (!this._haltRefreshId) {
            this._haltRefreshId = GLib.timeout_add_seconds(
                GLib.PRIORITY_DEFAULT,
                HALT_BORDER_REFRESH_SECONDS,
                () => {
                    if (!this._halted) {
                        this._haltRefreshId = 0;
                        return GLib.SOURCE_REMOVE;
                    }
                    this._showHaltBorder();
                    return GLib.SOURCE_CONTINUE;
                }
            );
        }
    }

    _clearHalt() {
        this._halted = false;
        this._haltEngagedAtUs = 0;
        if (this._haltRefreshId) {
            GLib.source_remove(this._haltRefreshId);
            this._haltRefreshId = 0;
        }
        if (this._indicator)
            this._indicator.clear();
    }

    _showHaltBorder() {
        if (this._indicator)
            this._indicator.show(HALT_LABEL, HALT_BORDER_TTL_SECONDS);
    }

    Halt() {
        this._engageHalt();
    }

    ClearHalt() {
        this._clearHalt();
    }

    HaltActive() {
        return this._halted;
    }

    // ------------------------------------------------------- activity border

    /* While halted the border IS the halt banner, so both verbs below refuse:
     * an agent must be able neither to retitle the banner away nor to clear
     * it via its ordinary activity-indicator calls. */

    SetIndicator(label, ttlSeconds) {
        if (this._halted)
            return false;
        return this._indicator ? this._indicator.show(label, ttlSeconds) : false;
    }

    ClearIndicator() {
        if (this._halted)
            return false;
        return this._indicator ? this._indicator.clear() : false;
    }

    IndicatorActive() {
        return this._indicator ? this._indicator.active : false;
    }
}
