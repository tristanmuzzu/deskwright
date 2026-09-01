/*
 * Computer Use Helpers — GNOME Shell extension.
 *
 * The compositor-side half of deskwright. Everything an agent needs
 * that GNOME on Wayland refuses to give an ordinary client -- screenshots,
 * window geometry and control, the pointer position, a trustworthy clipboard,
 * an on-screen activity border -- lives in dbus.js behind com.zeticle.deskwright.
 *
 * This file only wires it up, plus the one thing that must be registered from
 * here: the halt keybinding. It is registered with Main.wm.addKeybinding
 * because the documented route (a gsettings "custom keybinding" executed by
 * gsd-media-keys, which asks mutter to grab the accelerator over D-Bus) is
 * refused for every custom binding on GNOME 50 -- verified 2026-08-08,
 * including a bare unmodified F9:
 *     gsd-media-keys: Failed to grab accelerator for keybinding custom:.../ztest/
 * Registering from inside gnome-shell bypasses that path entirely.
 */

import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {DBusService} from './dbus.js';
import {sweepOrphanIndicators} from './indicator.js';

const HALT_KEY = 'halt-keybinding';

export default class ComputerUseHelpersExtension extends Extension {
    enable() {
        this._settings = this.getSettings();

        // Clear any border stranded by an earlier broken build before arming.
        try {
            sweepOrphanIndicators();
        } catch (e) {
            logError(e, 'deskwright: orphan indicator sweep');
        }

        this._dbus = new DBusService();
        this._dbus.enable();

        // The halt keybinding: the human kill switch. ActionMode.ALL so it
        // works from the overview, modal dialogs and the unlock dialog too --
        // this extension deliberately runs in unlock-dialog mode, and being
        // able to halt an agent is most valuable exactly when the screen is
        // locked and the human is not driving. IGNORE_AUTOREPEAT so holding
        // the combo cannot machine-gun the toggle.
        Main.wm.addKeybinding(
            HALT_KEY,
            this._settings,
            Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.ALL,
            () => this._dbus?.toggleHaltFromKeybinding()
        );
        this._haltKeyBound = true;
    }

    disable() {
        if (this._haltKeyBound) {
            Main.wm.removeKeybinding(HALT_KEY);
            this._haltKeyBound = false;
        }

        if (this._dbus) {
            this._dbus.disable();
            this._dbus = null;
        }

        this._settings = null;
    }
}
