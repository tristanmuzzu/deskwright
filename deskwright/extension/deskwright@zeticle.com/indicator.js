/*
 * The "an agent is driving this desktop" border.
 *
 * ydotool injects below the compositor and AT-SPI presses real widgets, so
 * nothing on screen distinguishes agent input from a human at the keyboard.
 * This paints an unmistakable edge while that is happening.
 *
 * It has to live inside gnome-shell. The obvious client-side route,
 * gtk4-layer-shell, was tried first and refused one layer down: mutter does not
 * implement wlr-layer-shell ("compositor does not support wlr-layer-shell",
 * verified 2026-08-13), and GNOME has no other always-on-top surface for a
 * Wayland client. Inside the shell it is one addChrome call.
 *
 * Two properties matter and both are deliberate:
 *   affectsInputRegion: false  -> clicks pass straight through to whatever is
 *       underneath, so the border can never wedge the desktop.
 *   trackFullscreen: false     -> it stays visible over fullscreen windows,
 *       which is exactly when you would most want to know.
 *
 * The border always carries a deadline. A crashed agent leaves something that
 * clears itself in seconds; a stuck border would only train you to ignore it.
 */

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const ORPHAN_NAME = 'wcu-indicator';

/* The border color. Deliberately NOT the orange of migration-helpers
 * (rgba(255, 122, 24, ...)): that extension's orphan sweep destroys any chrome
 * actor whose style contains its color string, so sharing the color would let
 * each extension's sweep silently kill the other's live border. A distinct hue
 * keeps the two sweeps disjoint and also tells a human which extension painted
 * the edge. */
const COLOR = 'rgba(36, 130, 255';

// Destroy any indicator actor left parented to the chrome by a previous,
// broken run. Without this the only cure for a stuck border is a logout,
// because a Wayland session cannot restart its own shell.
export function sweepOrphanIndicators() {
    let removed = 0;
    for (const actor of [...Main.layoutManager.uiGroup.get_children()]) {
        // Match by name OR by the border style. Actors stranded by builds from
        // before the naming existed carry no name, and matching only on name
        // left them on screen with no way to clear them short of a logout.
        // (The style string is ours alone -- see COLOR above -- so this can
        // never sweep migration-helpers' border.)
        let style = '';
        try {
            style = actor.get_style() ?? '';
        } catch (e) {
            style = '';
        }
        const looksLikeIndicator =
            actor.get_name() === ORPHAN_NAME ||
            style.includes(COLOR);
        if (looksLikeIndicator) {
            try {
                Main.layoutManager.removeChrome(actor);
            } catch (e) {
                // Not tracked as chrome; destroying it below is still correct.
            }
            actor.destroy();
            removed++;
        }
    }
    return removed;
}

const THICKNESS = 6;
const MAX_TTL_SECONDS = 300;
const MAX_LABEL = 80;

export class ActivityIndicator {
    constructor() {
        this._actors = [];
        this._timeoutId = 0;
        this._monitorsChangedId = 0;
        this._label = '';
    }

    destroy() {
        this.clear();
    }

    /**
     * Show (or refresh) the border for `ttl` seconds. Calling this again before
     * it expires just extends the deadline, which is what makes it cheap to
     * call on every single input action.
     */
    show(label, ttl) {
        const seconds = Math.min(Math.max(Number(ttl) || 0, 1), MAX_TTL_SECONDS);
        this._label = String(label || 'agent input').slice(0, MAX_LABEL);

        if (!this._actors.length)
            this._build();
        else
            this._retitle();

        if (this._timeoutId)
            GLib.source_remove(this._timeoutId);
        this._timeoutId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            Math.round(seconds * 1000),
            () => {
                this._timeoutId = 0;
                this.clear();
                return GLib.SOURCE_REMOVE;
            }
        );
        return true;
    }

    clear() {
        if (this._timeoutId) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }
        if (this._monitorsChangedId) {
            Main.layoutManager.disconnect(this._monitorsChangedId);
            this._monitorsChangedId = 0;
        }
        for (const actor of this._actors) {
            try {
                Main.layoutManager.removeChrome(actor);
                actor.destroy();
            } catch (e) {
                logError(e, 'deskwright: removing indicator chrome');
            }
        }
        this._actors = [];
        this._pills = [];
        return true;
    }

    get active() {
        return this._actors.length > 0;
    }

    // ------------------------------------------------------------------ paint

    _build() {
        this._pills = [];
        for (const monitor of Main.layoutManager.monitors) {
            const frame = new St.Widget({
                reactive: false,
                can_focus: false,
                track_hover: false,
                x: monitor.x,
                y: monitor.y,
                width: monitor.width,
                height: monitor.height,
                style: `border: ${THICKNESS}px solid ${COLOR}, 0.92);`,
                layout_manager: new Clutter.BinLayout(),
            });

            const pill = new St.Label({
                text: this._label,
                x_align: Clutter.ActorAlign.CENTER,
                y_align: Clutter.ActorAlign.START,
                style:
                    `background-color: ${COLOR}, 0.95);` +
                    'color: #001226; font-weight: bold; font-size: 12px;' +
                    'padding: 3px 14px 4px 14px;' +
                    'border-radius: 0 0 10px 10px;',
            });
            frame.add_child(pill);
            this._pills.push(pill);

            // GNOME 50 dropped 'affectsInputRegion' from addChrome's options and
            // throws on it. It threw AFTER the actor was already on screen but
            // BEFORE _actors.push below, so the border rendered with nothing
            // holding a reference to it: clear() then swept an empty list and
            // reported success while the border stayed up forever.
            // reactive = false gives the same click-through the option did.
            frame.reactive = false;
            frame.set_name(ORPHAN_NAME);
            Main.layoutManager.addChrome(frame, {
                affectsStruts: false,
                trackFullscreen: false,
            });

            // A slow breathe is far easier to notice in peripheral vision than a
            // static line, and costs one animation on an actor that never
            // repaints anything underneath it.
            try {
                frame.ease({
                    opacity: 150,
                    duration: 1100,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                    autoReverse: true,
                    repeatCount: -1,
                });
            } catch (e) {
                logError(e, 'deskwright: indicator pulse unavailable');
            }

            this._actors.push(frame);
        }

        // Hotplugging a monitor while the border is up would leave it sized for
        // a layout that no longer exists. Rebuild rather than guess.
        this._monitorsChangedId = Main.layoutManager.connect(
            'monitors-changed',
            () => {
                if (!this.active)
                    return;
                const label = this._label;
                const hadTimeout = this._timeoutId;
                this.clear();
                if (hadTimeout)
                    this.show(label, 8);
            }
        );
    }

    _retitle() {
        for (const pill of this._pills ?? []) {
            if (pill.text !== this._label)
                pill.text = this._label;
        }
    }
}
