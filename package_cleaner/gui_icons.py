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
