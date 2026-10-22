# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Erick Woods
"""GTK presentation assets; lazy imports keep the shared core GTK-independent."""
from pathlib import Path

APPLICATION_ICON = Path(__file__).resolve().parent.parent / 'data' / 'package-cleaner.svg'
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
ICONS = {
    'large': ('drive-harddisk-symbolic', 'drive-harddisk', 'applications-other'),
    'potential': ('document-open-recent-symbolic', 'document-open-recent', 'preferences-system-time'),
    'servers': ('applications-engineering-symbolic', 'applications-development-symbolic', 'applications-development'),
    'support': ('package-x-generic-symbolic', 'package-x-generic', 'applications-other'),
    'advanced': ('preferences-system-symbolic', 'preferences-system', 'applications-system'),
    'about': ('help-about-symbolic', 'help-about', 'dialog-information'),
    'search': ('edit-find-symbolic', 'edit-find'),
    'refresh': ('view-refresh-symbolic', 'view-refresh'),
    'queue': ('list-add-symbolic', 'list-add'),
    'keep': ('emblem-ok-symbolic', 'emblem-default', 'dialog-information'),
    'details': ('document-properties-symbolic', 'document-properties', 'dialog-information'),
    'cancel': ('process-stop-symbolic', 'process-stop'),
    'warning': ('dialog-warning-symbolic', 'dialog-warning'),
}


def icon_name(theme, role):
    """Never request an unavailable name; text remains if none are provided."""
    return next((name for name in ICONS[role] if theme.has_icon(name)), None)


def themed_image(role):
    from gi.repository import Gtk
    name = icon_name(Gtk.IconTheme.get_default(), role)
    if name is None:
        return None
    image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.MENU)
    image.set_pixel_size(16)
    return image


def decorate_button(button, role):
    image = themed_image(role)
    if image is not None:
        image.set_margin_end(4)
        button.set_image(image)
        button.set_always_show_image(True)


def application_pixbuf(size):
    from gi.repository import GdkPixbuf
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(str(APPLICATION_ICON), size, size, True)


APPLICATION_LIST_ICON_SIZE = 48
APPLICATION_FALLBACK_ICON = 'application-x-executable'


def application_list_image(icon):
    """A bounded logical icon slot; GTK handles aspect ratio, alpha and HiDPI.

    IconSize.DIALOG is only a lookup preference: a fixed-size theme entry can
    otherwise retain its native dimensions. Explicit pixel-size forces a fit
    (with transparent padding for non-square artwork), not a stretch.
    """
    from gi.repository import Gtk, Gio, GdkPixbuf, GLib
    image = Gtk.Image()
    image.set_halign(Gtk.Align.CENTER)
    image.set_valign(Gtk.Align.CENTER)
    theme = Gtk.IconTheme.get_default()

    def refresh(*_):
        scale = max(1, image.get_scale_factor())
        for candidate in (icon, APPLICATION_FALLBACK_ICON):
            try:
                if candidate and Path(candidate).is_absolute():
                    filename = str(candidate)
                    fmt, width, height = GdkPixbuf.Pixbuf.get_file_info(filename)
                    if fmt is None or width <= 0 or height <= 0:
                        continue
                    size = min(APPLICATION_LIST_ICON_SIZE, max(width, height))
                    # Validate decoding, not just the file header. Use a bounded
                    # decode and leave final display-scale rendering to GTK.
                    GdkPixbuf.Pixbuf.new_from_file_at_scale(filename, size * scale, size * scale, True)
                    gicon = Gio.FileIcon.new(Gio.File.new_for_path(filename))
                    image.set_from_gicon(gicon, Gtk.IconSize.DIALOG)
                else:
                    if not candidate:
                        continue
                    info = theme.lookup_icon_for_scale(candidate, APPLICATION_LIST_ICON_SIZE, scale, 0)
                    if info is None:
                        continue
                    pixbuf = info.load_icon()
                    # A small raster fallback should stay small, including on
                    # themes with no exact 48px entry. @2 assets use base_scale.
                    size = min(APPLICATION_LIST_ICON_SIZE,
                               max(1, max(pixbuf.get_width(), pixbuf.get_height()) // max(1, info.get_base_scale())))
                    image.set_from_icon_name(candidate, Gtk.IconSize.DIALOG)
                image.set_pixel_size(size)
                return
            except (GLib.Error, OSError, TypeError, ValueError):
                continue
        image.clear()  # Even a theme without the fallback must not break a row.
        image.set_pixel_size(APPLICATION_LIST_ICON_SIZE)

    image.connect('notify::scale-factor', refresh)
    changed = theme.connect('changed', refresh)
    image.connect('destroy', lambda *_: theme.disconnect(changed))
    refresh()
    return image
