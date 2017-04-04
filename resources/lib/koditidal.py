# -*- coding: utf-8 -*-
#
# Copyright (C) 2016 Arne Svenson
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import os, sys, re
import datetime
from urlparse import urlsplit
import xbmc
import xbmcvfs
import xbmcgui
import xbmcaddon
import xbmcplugin
from xbmcgui import ListItem
from routing import Plugin
from tidalapi import Config, Session, User, Favorites
from tidalapi.models import Quality, SubscriptionType, AlbumType, BrowsableMedia, Artist, Album, PlayableMedia, Track, Video, Playlist, Promotion, Category, CutInfo
from m3u8 import load as m3u8_load
from debug import DebugHelper

_addon_id = 'plugin.audio.tidal2'
addon = xbmcaddon.Addon(id=_addon_id)
plugin = Plugin(base_url = "plugin://" + _addon_id)
plugin.name = addon.getAddonInfo('name')
_addon_icon = os.path.join(addon.getAddonInfo('path'), 'icon.png')
_addon_fanart = os.path.join(addon.getAddonInfo('path'), 'fanart.jpg')

debug = DebugHelper(pluginName=plugin.name, 
                    detailLevel=2 if addon.getSetting('debug_log') == 'true' else 1, 
                    enableTidalApiLog= True if addon.getSetting('debug_log') == 'true' else False)
log = debug.log

try:
    KODI_VERSION = xbmc.getInfoLabel('System.BuildVersion').split()[0]
except:
    KODI_VERSION = '16.1'

CACHE_DIR = xbmc.translatePath(addon.getAddonInfo('profile')).decode('utf-8')
FAVORITES_FILE = os.path.join(CACHE_DIR, 'favorites.cfg')
PLAYLISTS_FILE = os.path.join(CACHE_DIR, 'playlists.cfg')
ALBUM_PLAYLIST_TAG = 'ALBUM'
VARIOUS_ARTIST_ID = '2935'


def _T(txtid):
    if isinstance(txtid, basestring):
        # Map TIDAL texts to Text IDs
        newid = {'artist':  30101, 'album':  30102, 'playlist':  30103, 'track':  30104, 'video':  30105,
                 'artists': 30101, 'albums': 30102, 'playlists': 30103, 'tracks': 30104, 'videos': 30105,
                 'featured': 30203, 'rising': 30211, 'discovery': 30212, 'movies': 30115, 'shows': 30116, 'genres': 30117, 'moods': 30118
                 }.get(txtid.lower(), None)
        if not newid: return txtid
        txtid = newid
    try:
        txt = addon.getLocalizedString(txtid)
        return txt
    except:
        return '%s' % txtid


def _P(key, default_txt=None):
    # Plurals of some Texts
    newid = {'new': 30111, 'local': 30112, 'exclusive': 30113, 'recommended': 30114, 'top': 30119,
             'artists': 30106, 'albums': 30107, 'playlists': 30108, 'tracks': 30109, 'videos': 30110
             }.get(key.lower(), None)
    if newid:
        return _T(newid)
    return default_txt if default_txt else key


# Convert TIDAL-API Media into Kodi List Items

class HasListItem(object):

    _is_logged_in = False

    def setLabelFormat(self):
        self._favorites_in_labels = True if addon.getSetting('favorites_in_labels') == 'true' else False
        self._user_playlists_in_labels = True if addon.getSetting('user_playlists_in_labels') == 'true' else False
        self.FOLDER_MASK = '{label}'
        if self._favorites_in_labels:
            self.FAVORITE_MASK = '<{label}>'
        else:
            self.FAVORITE_MASK = '{label}'
        self.STREAM_LOCKED_MASK = '{label} ({info})'
        if self._user_playlists_in_labels:
            self.USER_PLAYLIST_MASK = '{label} [{userpl}]'
        else:
            self.USER_PLAYLIST_MASK = '{label}'
        self.DEFAULT_PLAYLIST_MASK = '{label} ({mediatype})'
        self.MASTER_AUDIO_MASK = '{label} (MQA)'

    def getLabel(self, extended=True):
        return self.name

    def getListItem(self):
        li = ListItem(self.getLabel())
        if isinstance(self, PlayableMedia) and getattr(self, 'available', True):
            li.setProperty('isplayable', 'true')
        artwork = {'thumb': _addon_icon, 'fanart': _addon_fanart}
        if getattr(self, 'image', None):
            artwork['thumb'] = self.image
        if getattr(self, 'fanart', None):
            artwork['fanart'] = self.fanart
        li.setArt(artwork)
        # In Favorites View everything as a Favorite
        if self._is_logged_in and hasattr(self, '_isFavorite') and '/favorites/' in sys.argv[0]:
            self._isFavorite = True
        cm = self.getContextMenuItems()
        if len(cm) > 0:
            li.addContextMenuItems(cm)
        return li

    def getContextMenuItems(self):
        return []

    def getSortText(self, mode=None):
        return self.getLabel(extended=False)


class AlbumItem(Album, HasListItem):

    def __init__(self, item):
        self.__dict__.update(vars(item))
        self.artist = ArtistItem(self.artist)
        self.artists = [ArtistItem(artist) for artist in self.artists]
        self._ftArtists = [ArtistItem(artist) for artist in self._ftArtists]
        self._userplaylists = {}    # Filled by parser
        self._playlist_id = None    # ID of the Playlist
        self._playlist_pos = -1     # Item position in playlist
        self._etag = None           # ETag for User Playlists
        self._playlist_name = None  # Name of Playlist
        self._playlist_type = ''    # Playlist Type
        self._playlist_track_id = 0 # Track-ID of item which is shown as Album Item

    def getLabel(self, extended=True):
        self.setLabelFormat()
        label = self.getLongTitle()
        if extended and self._isFavorite and not '/favorites/' in sys.argv[0]:
            label = self.FAVORITE_MASK.format(label=label)
        label = '%s - %s' % (self.artist.getLabel(extended), label)
        txt = []
        plids = self._userplaylists.keys()
        for plid in plids:
            if plid <> self._playlist_id:
                txt.append('%s' % self._userplaylists.get(plid).get('title'))
        if extended and txt:
            label = self.USER_PLAYLIST_MASK.format(label=label, userpl=', '.join(txt))
        return label

    def getLongTitle(self):
        self.setLabelFormat()
        longTitle = self.title
        if self.type == AlbumType.ep:
            longTitle += ' (EP)'
        elif self.type == AlbumType.single:
            longTitle += ' (Single)'
        if getattr(self, 'year', None) and addon.getSetting('album_year_in_labels') == 'true':
            longTitle += ' (%s)' % self.year
        if self.isMasterAlbum and addon.getSetting('mqa_in_labels') == 'true':
            longTitle = self.MASTER_AUDIO_MASK.format(label=longTitle)
        return longTitle

    def getSortText(self, mode=None):
        return '%s - (%s) %s' % (self.artist.getLabel(extended=False), getattr(self, 'year', ''), self.getLongTitle())

    def getListItem(self):
        li = HasListItem.getListItem(self)
        url = plugin.url_for_path('/album/%s' % self.id)
        infoLabels = {
            'title': self.title,
            'album': self.title,
            'artist': self.artist.name,
            'year': getattr(self, 'year', None),
            'tracknumber': self._itemPosition + 1 if self._itemPosition >= 0 else 0,
        }
        if '17.' in KODI_VERSION:
            infoLabels.update({'mediatype': 'album',
                               'userrating': '%s' % int(round(self.popularity / 10.0))
                               })
        li.setInfo('music', infoLabels)
        return (url, li, True)

    def getContextMenuItems(self):
        cm = []
        if self._is_logged_in:
            if self._isFavorite:
                cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/albums/%s' % self.id)))
            else:
                cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/albums/%s' % self.id)))
            if self._playlist_type == 'USER':
                cm.append((_T(30240), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove/%s/%s' % (self._playlist_id, self._playlist_pos))))
                cm.append((_T(30248), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/move/%s/%s/%s' % (self._playlist_id, self._playlist_pos, self._playlist_track_id))))
            cm.append((_T(30239), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/add/album/%s' % self.id)))
            plids = self._userplaylists.keys()
            for plid in plids:
                if plid <> self._playlist_id:
                    cm.append(((_T(30247) % self._userplaylists[plid].get('title'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove_album/%s/%s' % (plid, self.id)))))
            cm.append((_T(30221), 'Container.Update(%s)' % plugin.url_for_path('/artist/%s' % self.artist.id)))
        return cm


class ArtistItem(Artist, HasListItem):

    def __init__(self, item):
        self.__dict__.update(vars(item))

    def getLabel(self, extended=True):
        self.setLabelFormat()
        if extended and self._isFavorite and not '/favorites/artists' in sys.argv[0]:
            return self.FAVORITE_MASK.format(label=self.name)
        return self.name

    def getListItem(self):
        li = HasListItem.getListItem(self)
        url = plugin.url_for_path('/artist/%s' % self.id)
        infoLabel = {'artist': self.name}
        if '17.' in KODI_VERSION:
            infoLabel.update({'mediatype': 'artist',
                              'userrating': '%s' % int(round(self.popularity / 10.0))
                              })
        li.setInfo('music', infoLabel)
        return (url, li, True)

    def getContextMenuItems(self):
        cm = []
        if self._is_logged_in:
            if self._isFavorite:
                cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/artists/%s' % self.id)))
            else:
                cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/artists/%s' % self.id)))
        return cm


class PlaylistItem(Playlist, HasListItem):

    def __init__(self, item):
        self.__dict__.update(vars(item))

    def getLabel(self, extended=True):
        self.setLabelFormat()
        label = self.name
        if extended and self._isFavorite and not '/favorites/' in sys.argv[0]:
            label = self.FAVORITE_MASK.format(label=label)
        if self.type == 'USER' and sys.argv[0].lower().find('user_playlists') >= 0:
            defaultpl = []
            if str(self.id) == addon.getSetting('default_trackplaylist_id'):
                defaultpl.append(_P('tracks'))
            if str(self.id) == addon.getSetting('default_videoplaylist_id'):
                defaultpl.append(_P('videos'))
            if str(self.id) == addon.getSetting('default_albumplaylist_id'):
                defaultpl.append(_P('albums'))
            if len(defaultpl) > 0:
                return self.DEFAULT_PLAYLIST_MASK.format(label=label, mediatype=', '.join(defaultpl))
        return label

    def getListItem(self):
        li = HasListItem.getListItem(self)
        path = '/playlist/%s/items/0'
        if self.type == 'USER' and ALBUM_PLAYLIST_TAG in self.description:
            path = '/playlist/%s/albums/0'
        url = plugin.url_for_path(path % self.id)
        infoLabel = {
            'artist': self.title,
            'album': self.description,
            'title': _T(30243).format(tracks=self.numberOfTracks, videos=self.numberOfVideos),
            'genre': _T(30243).format(tracks=self.numberOfTracks, videos=self.numberOfVideos),
            'tracknumber': self._itemPosition + 1 if self._itemPosition >= 0 else 0
        }
        if '17.' in KODI_VERSION:
            infoLabel.update({'userrating': '%s' % int(round(self.popularity / 10.0))})
        li.setInfo('music', infoLabel)
        return (url, li, True)

    def getContextMenuItems(self):
        cm = []
        if self.numberOfVideos > 0:
            cm.append((_T(30252), 'Container.Update(%s)' % plugin.url_for_path('/playlist/%s/tracks/0' % self.id)))
        if self.type == 'USER' and ALBUM_PLAYLIST_TAG in self.description:
            cm.append((_T(30254), 'Container.Update(%s)' % plugin.url_for_path('/playlist/%s/items/0' % self.id)))
        else:
            cm.append((_T(30255), 'Container.Update(%s)' % plugin.url_for_path('/playlist/%s/albums/0' % self.id)))
        if self._is_logged_in:
            if self.type == 'USER':
                cm.append((_T(30251), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/rename/%s' % self.id)))
                cm.append((_T(30235), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/delete/%s' % self.id)))
            else:
                if self._isFavorite:
                    cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/playlists/%s' % self.id)))
                else:
                    cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/playlists/%s' % self.id)))
            cm.append((_T(30239), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/add/playlist/%s' % self.id)))
            if self.type == 'USER' and sys.argv[0].lower().find('user_playlists') >= 0:
                if str(self.id) == addon.getSetting('default_trackplaylist_id'):
                    cm.append((_T(30250) % _T('Track'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_reset_default/tracks')))
                else:
                    cm.append((_T(30249) % _T('Track'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_set_default/tracks/%s' % self.id)))
                if str(self.id) == addon.getSetting('default_videoplaylist_id'):
                    cm.append((_T(30250) % _T('Video'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_reset_default/videos')))
                else:
                    cm.append((_T(30249) % _T('Video'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_set_default/videos/%s' % self.id)))
                if str(self.id) == addon.getSetting('default_albumplaylist_id'):
                    cm.append((_T(30250) % _T('Album'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_reset_default/albums')))
                else:
                    cm.append((_T(30249) % _T('Album'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist_set_default/albums/%s' % self.id)))
        return cm


class TrackItem(Track, HasListItem):

    def __init__(self, item):
        self.__dict__.update(vars(item))
        self.artist = ArtistItem(self.artist)
        self.artists = [ArtistItem(artist) for artist in self.artists]
        self._ftArtists = [ArtistItem(artist) for artist in self._ftArtists]
        self.album = AlbumItem(self.album)
        self._userplaylists = {} # Filled by parser

    def getLabel(self, extended=True):
        self.setLabelFormat()
        label1 = self.artist.getLabel(extended=extended if self.available else False)
        label2 = self.getLongTitle()
        if extended and self._isFavorite and self.available and not '/favorites/' in sys.argv[0]:
            label2 = self.FAVORITE_MASK.format(label=label2)
        label = '%s - %s' % (label1, label2)
        if extended and not self.available:
            label = self.STREAM_LOCKED_MASK.format(label=label, info=_T(30242))
        txt = []
        plids = self._userplaylists.keys()
        for plid in plids:
            if plid <> self._playlist_id:
                txt.append('%s' % self._userplaylists.get(plid).get('title'))
        if extended and txt:
            label = self.USER_PLAYLIST_MASK.format(label=label, userpl=', '.join(txt))
        return label

    def getLongTitle(self):
        self.setLabelFormat()
        longTitle = self.title
        if self.version and not self.version in self.title:
            longTitle += ' (%s)' % self.version
        if self.explicit and not 'Explicit' in self.title:
            longTitle += ' (Explicit)'
        if self.editable and isinstance(self._cut, CutInfo):
            if self._cut.name:
                longTitle += ' (%s)' % self._cut.name
        if self.album.isMasterAlbum and addon.getSetting('mqa_in_labels') == 'true':
            longTitle = self.MASTER_AUDIO_MASK.format(label=longTitle)
        return longTitle

    def getSortText(self, mode=None):
        if mode == 'ALBUM':
            return self.album.getSortText(mode=mode)
        return self.getLabel(extended=False)

    def getFtArtistsText(self):
        text = ''
        for item in self._ftArtists:
            if len(text) > 0:
                text = text + ', '
            text = text + item.name
        if len(text) > 0:
            text = 'ft. by ' + text
        return text

    def getComment(self):
        return self.getFtArtistsText()

    def getListItem(self):
        li = HasListItem.getListItem(self)
        if self.available:
            if isinstance(self._cut, CutInfo):
                url = plugin.url_for_path('/play_track_cut/%s/%s/%s' % (self.id, self._cut.id, self.album.id))
            else:
                url = plugin.url_for_path('/play_track/%s/%s' % (self.id, self.album.id))
            isFolder = False
        else:
            url = plugin.url_for_path('/stream_locked')
            isFolder = True
        infoLabel = {
            'title': self.title,
            'tracknumber': self._playlist_pos + 1 if self._playlist_id else self._itemPosition + 1 if self._itemPosition >= 0 else self.trackNumber,
            'discnumber': self.volumeNumber,
            'duration': self.duration,
            'artist': self.artist.name,
            'album': self.album.title,
            'year': getattr(self, 'year', None),
            'rating': '%s' % int(round(self.popularity / 20.0)),
            'comment': self.getComment()
        }
        if '17.' in KODI_VERSION:
            infoLabel.update({'mediatype': 'song',
                              'userrating': '%s' % int(round(self.popularity / 10.0))
                              })
        li.setInfo('music', infoLabel)
        return (url, li, isFolder)

    def getContextMenuItems(self):
        cm = []
        if self._is_logged_in:
            if self._isFavorite:
                cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/tracks/%s' % self.id)))
            else:
                cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/tracks/%s' % self.id)))
            if self._playlist_type == 'USER':
                cm.append((_T(30240), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove/%s/%s' % (self._playlist_id, self._playlist_pos))))
                item_id = self.id if not isinstance(self._cut, CutInfo) else self._cut.id
                cm.append((_T(30248), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/move/%s/%s/%s' % (self._playlist_id, self._playlist_pos, item_id))))
            else:
                cm.append((_T(30239), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/add/track/%s' % self.id)))
            plids = self._userplaylists.keys()
            for plid in plids:
                if plid <> self._playlist_id:
                    playlist = self._userplaylists[plid]
                    if '%s' % self.album.id in playlist.get('album_ids', []):
                        cm.append(((_T(30247) % playlist.get('title'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove_album/%s/%s' % (plid, self.album.id)))))
                    else:
                        cm.append(((_T(30247) % playlist.get('title'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove_id/%s/%s' % (plid, self.id)))))
        cm.append((_T(30221), 'Container.Update(%s)' % plugin.url_for_path('/artist/%s' % self.artist.id)))
        cm.append((_T(30245), 'Container.Update(%s)' % plugin.url_for_path('/album/%s' % self.album.id)))
        cm.append((_T(30222), 'Container.Update(%s)' % plugin.url_for_path('/track_radio/%s' % self.id)))
        cm.append((_T(30223), 'Container.Update(%s)' % plugin.url_for_path('/recommended/tracks/%s' % self.id)))
        return cm


class VideoItem(Video, HasListItem):

    def __init__(self, item):
        self.__dict__.update(vars(item))
        self.artist = ArtistItem(self.artist)
        self.artists = [ArtistItem(artist) for artist in self.artists]
        self._ftArtists = [ArtistItem(artist) for artist in self._ftArtists]
        self._userplaylists = {} # Filled by parser

    def getLabel(self, extended=True):
        self.setLabelFormat()
        label1 = self.artist.name
        if extended and self.artist._isFavorite and self.available:
            label1 = self.FAVORITE_MASK.format(label=label1)
        label2 = self.getLongTitle()
        if extended and self._isFavorite and self.available and not '/favorites/' in sys.argv[0]:
            label2 = self.FAVORITE_MASK.format(label=label2)
        label = '%s - %s' % (label1, label2)
        if extended and not self.available:
            label = self.STREAM_LOCKED_MASK.format(label=label, info=_T(30242))
        txt = []
        plids = self._userplaylists.keys()
        for plid in plids:
            if plid <> self._playlist_id:
                txt.append('%s' % self._userplaylists.get(plid).get('title'))
        if extended and txt:
            label = self.USER_PLAYLIST_MASK.format(label=label, userpl=', '.join(txt))
        return label

    def getLongTitle(self):
        longTitle = self.title
        if self.explicit and not 'Explicit' in self.title:
            longTitle += ' (Explicit)'
        if getattr(self, 'year', None):
            longTitle += ' (%s)' % self.year
        return longTitle

    def getFtArtistsText(self):
        text = ''
        for item in self._ftArtists:
            if len(text) > 0:
                text = text + ', '
            text = text + item.name
        if len(text) > 0:
            text = 'ft. by ' + text
        return text

    def getComment(self):
        return self.getFtArtistsText()

    def getListItem(self):
        li = HasListItem.getListItem(self)
        if self.available:
            url = plugin.url_for_path('/play_video/%s' % self.id)
            isFolder = False
        else:
            url = plugin.url_for_path('/stream_locked')
            isFolder = True
        infoLabel = {
            'artist': [self.artist.name],
            'title': self.title,
            'tracknumber': self._playlist_pos + 1 if self._playlist_id else self._itemPosition + 1,
            'year': getattr(self, 'year', None),
            'plotoutline': self.getComment(),
            'plot': self.getFtArtistsText()
        }
        if '17.' in KODI_VERSION:
            infoLabel.update({'mediatype': 'musicvideo',
                              'userrating': '%s' % int(round(self.popularity / 10.0))
                              })
        li.setInfo('video', infoLabel)
        li.addStreamInfo('video', { 'codec': 'h264', 'aspect': 1.78, 'width': 1920,
                         'height': 1080, 'duration': self.duration })
        li.addStreamInfo('audio', { 'codec': 'AAC', 'language': 'en', 'channels': 2 })
        return (url, li, isFolder)

    def getContextMenuItems(self):
        cm = []
        if self._is_logged_in:
            if self._isFavorite:
                cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/videos/%s' % self.id)))
            else:
                cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/videos/%s' % self.id)))
            if self._playlist_type == 'USER':
                cm.append((_T(30240), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove/%s/%s' % (self._playlist_id, self._playlist_pos))))
                cm.append((_T(30248), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/move/%s/%s/%s' % (self._playlist_id, self._playlist_pos, self.id))))
            else:
                cm.append((_T(30239), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/add/video/%s' % self.id)))
            plids = self._userplaylists.keys()
            for plid in plids:
                if plid <> self._playlist_id:
                    cm.append(((_T(30247) % self._userplaylists[plid].get('title'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove_id/%s/%s' % (plid, self.id)))))
        cm.append((_T(30221), 'Container.Update(%s)' % plugin.url_for_path('/artist/%s' % self.artist.id)))
        cm.append((_T(30224), 'Container.Update(%s)' % plugin.url_for_path('/recommended/videos/%s' % self.id)))
        return cm


class PromotionItem(Promotion, HasListItem):

    def __init__(self, item):
        if item.type != 'EXTURL' and item.id.startswith('http:'):
            item.type = 'EXTURL' # Fix some defect TIDAL Promotions
        self.__dict__.update(vars(item))
        self._userplaylists = {} # Filled by parser

    def getLabel(self, extended=True):
        self.setLabelFormat()
        if self.type in ['ALBUM', 'VIDEO']:
            label = '%s - %s' % (self.shortHeader, self.shortSubHeader)
        else:
            label = self.shortHeader
        if extended and self._isFavorite:
            label = self.FAVORITE_MASK.format(label=label)
        txt = []
        plids = self._userplaylists.keys()
        for plid in plids:
            txt.append('%s' % self._userplaylists.get(plid).get('title'))
        if extended and txt:
            label = self.USER_PLAYLIST_MASK.format(label=label, userpl=', '.join(txt))
        return label

    def getListItem(self):
        li = HasListItem.getListItem(self)
        isFolder = True
        if self.type == 'PLAYLIST':
            url = plugin.url_for_path('/playlist/%s/items/0' % self.id)
            infoLabel = {
                'artist': self.shortHeader,
                'album': self.text,
                'title': self.shortSubHeader
            }
            if '17.' in KODI_VERSION:
                infoLabel.update({'userrating': '%s' % int(round(self.popularity / 10.0))})
            li.setInfo('music', infoLabel)
        elif self.type == 'ALBUM':
            url = plugin.url_for_path('/album/%s' % self.id)
            infoLabel = {
                'artist': self.shortHeader,
                'album': self.text,
                'title': self.shortSubHeader
            }
            if '17.' in KODI_VERSION:
                infoLabel.update({'mediatype': 'album',
                                  'userrating': '%s' % int(round(self.popularity / 10.0))
                                  })
            li.setInfo('music', infoLabel)
        elif self.type == 'VIDEO':
            url = plugin.url_for_path('/play_video/%s' % self.id)
            infoLabel = {
                'artist': [self.shortHeader],
                'album': self.text,
                'title': self.shortSubHeader
            }
            if '17.' in KODI_VERSION:
                infoLabel.update({'mediatype': 'musicvideo',
                                  'userrating': '%s' % int(round(self.popularity / 10.0))
                                  })
            li.setInfo('video', infoLabel)
            li.setProperty('isplayable', 'true')
            isFolder = False
            li.addStreamInfo('video', { 'codec': 'h264', 'aspect': 1.78, 'width': 1920,
                             'height': 1080, 'duration': self.duration })
            li.addStreamInfo('audio', { 'codec': 'AAC', 'language': 'en', 'channels': 2 })
        else:
            return (None, None, False)
        return (url, li, isFolder)

    def getContextMenuItems(self):
        cm = []
        if self.type == 'PLAYLIST':
            if self._is_logged_in:
                if self._isFavorite:
                    cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/playlists/%s' % self.id)))
                else:
                    cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/playlists/%s' % self.id)))
            cm.append((_T(30255), 'Container.Update(%s)' % plugin.url_for_path('/playlist/%s/albums/0' % self.id)))
        elif self.type == 'ALBUM':
            if self._is_logged_in:
                if self._isFavorite:
                    cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/albums/%s' % self.id)))
                else:
                    cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/albums/%s' % self.id)))
        elif self.type == 'VIDEO':
            if self._is_logged_in:
                if self._isFavorite:
                    cm.append((_T(30220), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/remove/videos/%s' % self.id)))
                else:
                    cm.append((_T(30219), 'RunPlugin(%s)' % plugin.url_for_path('/favorites/add/videos/%s' % self.id)))
                cm.append((_T(30239), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/add/video/%s' % self.id)))
            plids = self._userplaylists.keys()
            for plid in plids:
                cm.append(((_T(30247) % self._userplaylists[plid].get('title'), 'RunPlugin(%s)' % plugin.url_for_path('/user_playlist/remove_id/%s/%s' % (plid, self.id)))))
            cm.append((_T(30224), 'Container.Update(%s)' % plugin.url_for_path('/recommended/videos/%s' % self.id)))
        return cm


class CategoryItem(Category, HasListItem):

    _force_subfolders = False
    _label = None

    def __init__(self, item):
        self.__dict__.update(vars(item))

    def getLabel(self, extended=True):
        self.setLabelFormat()
        if extended:
            return self.FOLDER_MASK.format(label=self._label)
        return self._label

    def getListItems(self):
        content_types = self.content_types
        items = []
        if len(content_types) > 1 and self._group in ['moods', 'genres'] and not self._force_subfolders:
            # Use sub folders for multiple Content Types
            url = plugin.url_for_path('/category/%s/%s' % (self._group, self.path))
            self._label = _P(self.path, self.name)
            li = HasListItem.getListItem(self)
            li.setInfo('music', {
                'artist': self._label
            })
            items.append((url, li, True))
        else:
            for content_type in content_types:
                url = plugin.url_for_path('/category/%s/%s/%s/%s' % (self._group, self.path, content_type, 0))
                if len(content_types) > 1:
                    if self._force_subfolders:
                        # Show only Content Type as sub folders
                        self._label = _P(content_type)
                    else:
                        # Show Path and Content Type as sub folder
                        self._label = '%s %s' % (_P(self.path, self.name), _P(content_type))
                else:
                    # Use Path as folder because content type is shows as sub foldes
                    self._label = _P(self.path, self.name)
                li = HasListItem.getListItem(self)
                li.setInfo('music', {
                    'artist': _P(self.path, self.name),
                    'album': _P(content_type)
                })
                items.append((url, li, True))
        return items


class FolderItem(BrowsableMedia, HasListItem):

    def __init__(self, label, url, thumb=None, fanart=None, isFolder=True, otherLabel=None):
        self.name = label
        self._url = url
        self._thumb = thumb
        self._fanart = fanart
        self._isFolder = isFolder
        self._otherLabel = otherLabel

    def getLabel(self, extended=True):
        self.setLabelFormat()
        label = self._otherLabel if self._otherLabel else self.name
        if extended:
            label = self.FOLDER_MASK.format(label=label)
        return label

    def getListItem(self):
        li = HasListItem.getListItem(self)
        li.setInfo('music', {
            'artist': self.name
        })
        return (self._url, li, self._isFolder)

    @property
    def image(self):
        return self._thumb if self._thumb else HasListItem.image

    @property
    def fanart(self):
        return self._fanart if self._fanart else HasListItem.fanart


class LoginToken(object):

    browser =   'wdgaB1CilGA-S_s2' # Streams HIGH/LOW Quality over RTMP, FLAC and Videos over HTTP, but many Lossless Streams are encrypted.
    android =   'kgsOOmYk3zShYrNP' # All Streams are HTTP Streams. Correct numberOfVideos in Playlists (best Token to use)
    ios =       '_DSTon1kC8pABnTw' # Same as Android Token, but uses ALAC instead of FLAC
    native =    '4zx46pyr9o8qZNRw' # Same as Android Token, but FLAC streams are encrypted
    audirvana = 'BI218mwp9ERZ3PFI' # Like Android Token, supports MQA, but returns 'numberOfVideos = 0' in Playlists
    amarra =    'wc8j_yBJd20zOmx0' # Like Android Token, but returns 'numberOfVideos = 0' in Playlists
    # Unkown working Tokens
    token1 =    'P5Xbeo5LFvESeDy6' # Like Android Token, but returns 'numberOfVideos = 0' in Playlists
    token2 =    'oIaGpqT_vQPnTr0Q' # Like token1, nut uses RTMP for HIGH/LOW Quality
    token3 =    '_KM2HixcUBZtmktH' # Same as token1

    features = {
        # token: Login-Token to get a Session-ID
        # codecs: Supported Audio Codecs without encryption
        # rtmp: Uses RTMP Protocol for HIGH/LOW Quality Audio Streams
        # videosInPlaylists: True: numberOfVideos in Playlists is correct, False: returns 'numberOfVideos = 0' in Playlists
        # user-agent: Special User-Agent in HTTP-Request-Header
        'browser':   { 'token': browser,   'codecs': ['AAC'],                'rtmp': True,  'videosInPlaylists': True,  'user-agent': None },
        'android':   { 'token': android,   'codecs': ['AAC', 'FLAC'],        'rtmp': False, 'videosInPlaylists': True,  'user-agent': 'TIDAL_ANDROID/686 okhttp/3.3.1' },
        'ios':       { 'token': ios,       'codecs': ['AAC', 'ALAC'],        'rtmp': False, 'videosInPlaylists': True,  'user-agent': 'TIDAL/546 CFNetwork/808.2.16 Darwin/16.3.0' },
        'native':    { 'token': native,    'codecs': ['AAC'],                'rtmp': False, 'videosInPlaylists': True,  'user-agent': 'TIDAL_NATIVE_PLAYER/OSX/2.3.20' },
        'audirvana': { 'token': audirvana, 'codecs': ['AAC', 'FLAC', 'MQA'], 'rtmp': False, 'videosInPlaylists': False, 'user-agent': 'Audirvana Plus/2.6.4 CFNetwork/807.2.14 Darwin/16.3.0 (x86_64)' },
        'amarra':    { 'token': amarra,    'codecs': ['AAC', 'FLAC'],        'rtmp': False, 'videosInPlaylists': False, 'user-agent': 'Amarra for TIDAL/2.2.1261 CFNetwork/807.2.14 Darwin/16.3.0 (x86_64)' },
        # Unknown working Tokens
        'token1':    { 'token': token1,    'codecs': ['AAC', 'FLAC'],        'rtmp': False, 'videosInPlaylists': False, 'user-agent': None },
        'token2':    { 'token': token2,    'codecs': ['AAC', 'FLAC'],        'rtmp': True,  'videosInPlaylists': False, 'user-agent': None },
        'token3':    { 'token': token3,    'codecs': ['AAC', 'FLAC'],        'rtmp': False, 'videosInPlaylists': False, 'user-agent': None }
    }

    priority = ['android', 'ios', 'audirvana', 'browser', 'native', 'amarra', 'token1', 'token2', 'token3']

    @staticmethod
    def getFeatures(tokenName='android'):
        return LoginToken.features.get(tokenName)

    @staticmethod
    def getToken(tokenName='android'):
        return LoginToken.getFeatures(tokenName).get('token')

    @staticmethod
    def select(codec, rtmp=False, api=True):
        tokens = []
        lossless = codec in ['FLAC', 'ALAC', 'MQA']
        rtmp_relevant = False if lossless else True
        for tokenName in LoginToken.priority:
            token = LoginToken.getFeatures(tokenName)
            if codec in token.get('codecs') and (not rtmp_relevant or token.get('rtmp') == rtmp) and (not api or token.get('videosInPlaylists') == api):
                tokens.append(tokenName)
        return tokens


# Session from the TIDAL-API to parse Items into Kodi List Items

class TidalConfig(Config):

    def __init__(self):
        Config.__init__(self)
        self.load()

    def load(self):
        self.session_id = addon.getSetting('session_id')
        self.stream_session_id = addon.getSetting('stream_session_id')
        if not self.stream_session_id:
            self.stream_session_id = self.session_id
        self.country_code = addon.getSetting('country_code')
        self.user_id = addon.getSetting('user_id')
        self.subscription_type = [SubscriptionType.hifi, SubscriptionType.premium][min(1, int('0' + addon.getSetting('subscription_type')))]
        self.client_unique_key = addon.getSetting('client_unique_key')
        self.quality = [Quality.lossless, Quality.high, Quality.low][min(2, int('0' + addon.getSetting('quality')))]
        self.use_rtmp = True if addon.getSetting('music_option') == '3' and self.quality <> Quality.lossless else False
        self.codec = ['FLAC', 'AAC', 'AAC'][min([2, int('0' + addon.getSetting('quality'))])]
        if addon.getSetting('music_option') == '1' and self.quality == Quality.lossless:
            self.codec = 'ALAC'
        elif addon.getSetting('music_option') == '2' and self.quality == Quality.lossless:
            self.codec = 'MQA'
        self.maxVideoHeight = [9999, 1080, 720, 540, 480, 360, 240][min(6, int('0%s' % addon.getSetting('video_quality')))]
        self.pageSize = max(10, min(9999, int('0%s' % addon.getSetting('page_size'))))
        self.debug = True if addon.getSetting('debug_log') == 'true' else False
        self.debug_json = True if addon.getSetting('debug_json') == 'true' else False

class TidalSession(Session):

    errorCodes = []

    def __init__(self, config=TidalConfig()):
        Session.__init__(self, config=config)

    def init_user(self, user_id, subscription_type):
        return TidalUser(self, user_id, subscription_type)

    def load_session(self):
        if not self._config.country_code:
            self._config.country_code = self.local_country_code()
            addon.setSetting('country_code', self._config.country_code)
        Session.load_session(self, self._config.session_id, self._config.country_code, self._config.user_id,
                             self._config.subscription_type, self._config.client_unique_key)
        self.stream_session_id = self._config.stream_session_id

    def generate_client_unique_key(self):
        unique_key = addon.getSetting('client_unique_key')
        if not unique_key:
            unique_key = Session.generate_client_unique_key(self)
        return unique_key

    def login_with_token(self, username, password, subscription_type, tokenName, api=True):
        old_token = self._config.api_token
        old_session_id = self.session_id
        self._config.api_token = LoginToken.getToken(tokenName)
        self.session_id = None
        Session.login(self, username, password, subscription_type)
        success = True if self.session_id else False
        if not api:
            self.stream_session_id = self.session_id
            if old_session_id:
                self.session_id = old_session_id
        self._config.api_token = old_token
        return success

    def login(self, username, password, subscription_type=None):
        if not username or not password:
            return False
        if not subscription_type:
            # Set Subscription Type corresponding to the given playback quality
            subscription_type = SubscriptionType.hifi if self._config.quality == Quality.lossless else SubscriptionType.premium
        if not self.client_unique_key:
            # Generate a random client key if no key is given
            self.client_unique_key = self.generate_client_unique_key()
        api_token = ''
        # Get working Tokens with correct numberOfVideos in Playlists which can be used for API calls and for Streaming
        tokenNames = LoginToken.select(codec=self._config.codec, rtmp=self._config.use_rtmp, api=True)
        if not tokenNames:
            # Get a default API Token
            tokenNames = LoginToken.select(codec='AAC', rtmp=self._config.use_rtmp, api=True)
        for tokenName in tokenNames:
            loginOk = self.login_with_token(username, password, subscription_type, tokenName, api=True)
            if loginOk:
                self.stream_session_id = self.session_id
                api_token = tokenName
                break
        # Get Tokens which are necessary for Streaming
        tokenNames = LoginToken.select(codec=self._config.codec, rtmp=self._config.use_rtmp, api=False)
        if api_token not in tokenNames:
            # Get Session-ID for Streaming
            for tokenName in tokenNames:
                loginOk = self.login_with_token(username, password, subscription_type, tokenName, api=False)
                if loginOk:
                    break
        # Save Session Data into Addon-Settings
        if self.is_logged_in:
            addon.setSetting('session_id', self.session_id)
            addon.setSetting('stream_session_id', self.stream_session_id)
            addon.setSetting('country_code', self.country_code)
            addon.setSetting('user_id', unicode(self.user.id))
            addon.setSetting('subscription_type', '0' if self.user.subscription.type == SubscriptionType.hifi else '1')
            addon.setSetting('client_unique_key', self.client_unique_key)
            self._config.load()
        return self.is_logged_in

    def logout(self):
        Session.logout(self)
        self.stream_session_id = None
        addon.setSetting('session_id', '')
        addon.setSetting('stream_session_id', '')
        addon.setSetting('user_id', '')
        self._config.load()

    def get_album_tracks(self, album_id, withAlbum=True):
        items = Session.get_album_tracks(self, album_id)
        if withAlbum:
            album = self.get_album(album_id)
            if album:
                for item in items:
                    item.album = album
        return items

    def get_playlist_tracks(self, playlist_id, offset=0, limit=9999):
        # keeping 1st parameter as playlist_id for backward compatibility 
        if isinstance(playlist_id, Playlist):
            playlist = playlist_id
            playlist_id = playlist.id
        else:
            playlist = self.get_playlist(playlist_id)
        # Don't read empty playlists
        if not playlist or playlist.numberOfItems == 0:
            return []
        items = Session.get_playlist_tracks(self, playlist.id, offset=offset, limit=limit)
        if items:
            for item in items:
                item._etag = playlist._etag
                item._playlist_name = playlist.title
                item._playlist_type = playlist.type
        return items

    def get_item_albums(self, items):
        albums = []
        for item in items:
            album = item.album
            if not album.releaseDate:
                album.releaseDate = item.streamStartDate
            # Item-Position in the Kodi-List (filled by _map_request)
            album._itemPosition = item._itemPosition
            album._offset = item._offset
            album._totalNumberOfItems = item._totalNumberOfItems
            # Infos for Playlist-Item-Position (filled by get_playlist_tracks, get_playlist_items)
            album._playlist_id = item._playlist_id
            album._playlist_pos = item._playlist_pos
            album._etag = item._etag
            album._playlist_name = item._playlist_name
            album._playlist_type = item._playlist_type
            # Track-ID in TIDAL-Playlist
            album._playlist_track_id = item.id
            albums.append(album)
        return albums

    def get_playlist_albums(self, playlist, offset=0, limit=9999):
        return self.get_item_albums(self.get_playlist_tracks(self, playlist, offset=offset, limit=limit))

    def get_artist_top_tracks(self, artist_id, offset=0, limit=999):
        items = Session.get_artist_top_tracks(self, artist_id, offset=offset, limit=limit)
        if not items and limit >= 100:
            items = Session.get_artist_top_tracks(self, artist_id, offset=offset, limit=100)
        if not items and limit >= 50:
            items = Session.get_artist_top_tracks(self, artist_id, offset=offset, limit=50)
        if not items:
            items = Session.get_artist_top_tracks(self, artist_id, offset=offset, limit=20)
        return items

    def get_artist_radio(self, artist_id, offset=0, limit=999):
        items = Session.get_artist_radio(self, artist_id, offset=offset, limit=limit)
        if not items and limit >= 100:
            items = Session.get_artist_radio(self, artist_id, offset=offset, limit=100)
        if not items and limit >= 50:
            items = Session.get_artist_radio(self, artist_id, offset=offset, limit=50)
        if not items:
            items = Session.get_artist_radio(self, artist_id, offset=offset, limit=20)
        return items

    def get_track_radio(self, track_id, offset=0, limit=999):
        items = Session.get_track_radio(self, track_id, offset=offset, limit=limit)
        if not items and limit >= 100:
            items = Session.get_track_radio(self, track_id, offset=offset, limit=100)
        if not items and limit >= 50:
            items = Session.get_track_radio(self, track_id, offset=offset, limit=50)
        if not items:
            items = Session.get_track_radio(self, track_id, offset=offset, limit=20)
        return items

    def get_recommended_items(self, content_type, item_id, offset=0, limit=999):
        items = Session.get_recommended_items(self, content_type, item_id, offset=offset, limit=limit)
        if not items and limit >= 100:
            items = Session.get_recommended_items(self, content_type, item_id, offset=offset, limit=100)
        if not items and limit >= 50:
            items = Session.get_recommended_items(self, content_type, item_id, offset=offset, limit=50)
        if not items:
            items = Session.get_recommended_items(self, content_type, item_id, offset=offset, limit=20)
        return items

    def _parse_album(self, json_obj, artist=None):
        album = AlbumItem(Session._parse_album(self, json_obj, artist=artist))
        album._is_logged_in = self.is_logged_in
        if self.is_logged_in:
            album._userplaylists = self.user.playlists_of_id(None, album.id)
        return album

    def _parse_artist(self, json_obj):
        artist = ArtistItem(Session._parse_artist(self, json_obj))
        artist._is_logged_in = self.is_logged_in
        return artist

    def _parse_playlist(self, json_obj):
        playlist = PlaylistItem(Session._parse_playlist(self, json_obj))
        playlist._is_logged_in = self.is_logged_in
        return playlist

    def _parse_track(self, json_obj):
        track = TrackItem(Session._parse_track(self, json_obj))
        if not getattr(track.album, 'streamStartDate', None):
            track.album.streamStartDate = track.streamStartDate
        track._is_logged_in = self.is_logged_in
        if self.is_logged_in:
            track._userplaylists = self.user.playlists_of_id(track.id, track.album.id)
        elif track.duration > 30:
            # 30 Seconds Limit in Trial Mode
            track.duration = 30
        return track

    def _parse_video(self, json_obj):
        video = VideoItem(Session._parse_video(self, json_obj))
        video._is_logged_in = self.is_logged_in
        if self.is_logged_in:
            video._userplaylists = self.user.playlists_of_id(video.id)
        elif video.duration > 30:
            # 30 Seconds Limit in Trial Mode
            video.duration = 30
        return video

    def _parse_promotion(self, json_obj):
        promotion = PromotionItem(Session._parse_promotion(self, json_obj))
        promotion._is_logged_in = self.is_logged_in
        if self.is_logged_in and promotion.type == 'VIDEO':
            promotion._userplaylists = self.user.playlists_of_id(promotion.id)
        return promotion

    def _parse_category(self, json_obj):
        return CategoryItem(Session._parse_category(self, json_obj))

    def get_media_url(self, track_id, quality=None, cut_id=None, fallback=False):
        return Session.get_media_url(self, track_id, quality=quality, cut_id=cut_id, fallback=fallback)

    def get_track_url(self, track_id, quality=None, cut_id=None, fallback=True):
        oldSessionId = self.session_id
        self.session_id = self.stream_session_id
        soundQuality = quality if quality else self._config.quality
        media = Session.get_track_url(self, track_id, quality=soundQuality, cut_id=cut_id)
        if fallback and soundQuality == Quality.lossless and (media == None or media.isEncrypted):
            log(media.url, level=xbmc.LOGWARNING)
            if media:
                log('Got encryptionKey "%s" for track %s, trying HIGH Quality ...' % (media.encryptionKey, track_id), level=xbmc.LOGWARNING)
            else:
                log('No Lossless stream for track %s, trying HIGH Quality ...' % track_id, level=xbmc.LOGWARNING)
            media = self.get_track_url(track_id, quality=Quality.high, cut_id=cut_id, fallback=False)
        if media:
            if quality == Quality.lossless and media.codec not in ['FLAC', 'ALAC', 'MQA']:
                xbmcgui.Dialog().notification(plugin.name, _T(30504) , icon=xbmcgui.NOTIFICATION_WARNING)
            log('Got stream with soundQuality:%s, codec:%s' % (media.soundQuality, media.codec))
        self.session_id = oldSessionId
        return media

    def get_video_url(self, video_id, maxHeight=-1):
        oldSessionId = self.session_id
        self.session_id = self.stream_session_id
        media = Session.get_video_url(self, video_id)
        maxVideoHeight = maxHeight if maxHeight > 0 else self._config.maxVideoHeight
        if maxVideoHeight <> 9999 and media.url.lower().find('.m3u8') > 0:
            log('Parsing M3U8 Playlist: %s' % media.url)
            m3u8obj = m3u8_load(media.url)
            if m3u8obj.is_variant and not m3u8obj.cookies:
                # Variant Streams with Cookies have to be played without stream selection.
                # You can change the Bandwidth Limit in Kodi Settings to select other streams !
                # Select stream with highest resolution <= maxVideoHeight
                selected_height = 0
                for playlist in m3u8obj.playlists:
                    try:
                        width, height = playlist.stream_info.resolution
                        if height > selected_height and height <= maxVideoHeight:
                            if re.match(r'https?://', playlist.uri):
                                media.url = playlist.uri
                            else:
                                media.url = m3u8obj.base_uri + playlist.uri
                            selected_height = height
                            media.width = width
                            media.height = height
                    except:
                        pass
        self.session_id = oldSessionId
        return media

    def add_list_items(self, items, content=None, end=True, withNextPage=False):
        if content:
            xbmcplugin.setContent(plugin.handle, content)
        list_items = []
        for item in items:
            if isinstance(item, Category):
                category_items = item.getListItems()
                for url, li, isFolder in category_items:
                    if url and li:
                        list_items.append((url, li, isFolder))
            elif isinstance(item, BrowsableMedia):
                url, li, isFolder = item.getListItem()
                if url and li:
                    list_items.append((url, li, isFolder))
        if withNextPage and len(items) > 0:
            # Add folder for next page
            try:
                totalNumberOfItems = items[0]._totalNumberOfItems
                nextOffset = items[0]._offset + self._config.pageSize
                if nextOffset < totalNumberOfItems and len(items) >= self._config.pageSize:
                    path = urlsplit(sys.argv[0]).path or '/'
                    path = path.split('/')[:-1]
                    path.append(str(nextOffset))
                    url = '/'.join(path)
                    self.add_directory_item(_T(30244).format(pos1=nextOffset, pos2=min(nextOffset+self._config.pageSize, totalNumberOfItems)), plugin.url_for_path(url))
            except:
                log('Next Page for URL %s not set' % sys.argv[0], xbmc.LOGERROR)
        if len(list_items) > 0:
            xbmcplugin.addDirectoryItems(plugin.handle, list_items)
        if end:
            xbmcplugin.endOfDirectory(plugin.handle)

    def add_directory_item(self, title, endpoint, thumb=None, fanart=None, end=False, isFolder=True):
        if callable(endpoint):
            endpoint = plugin.url_for(endpoint)
        item = FolderItem(title, endpoint, thumb, fanart, isFolder)
        self.add_list_items([item], end=end)


class TidalFavorites(Favorites):

    def __init__(self, session, user_id):
        Favorites.__init__(self, session, user_id)

    def load_cache(self):
        try:
            fd = xbmcvfs.File(FAVORITES_FILE, 'r')
            self.ids_content = fd.read()
            self.ids = eval(self.ids_content)
            fd.close()
            self.ids_loaded = not (self.ids['artists'] == None or self.ids['albums'] == None or
                                   self.ids['playlists'] == None or self.ids['tracks'] == None or
                                   self.ids['videos'] == None)
            if self.ids_loaded:
                log('Loaded %s Favorites from disk.' % sum(len(self.ids[content]) for content in ['artists', 'albums', 'playlists', 'tracks', 'videos']))
        except:
            self.ids_loaded = False
            self.reset()
        return self.ids_loaded

    def save_cache(self):
        try:
            if self.ids_loaded:
                new_ids = repr(self.ids)
                if new_ids <> self.ids_content:
                    fd = xbmcvfs.File(FAVORITES_FILE, 'w')
                    fd.write(new_ids)
                    fd.close()
                    log('Saved %s Favorites to disk.' % sum(len(self.ids[content]) for content in ['artists', 'albums', 'playlists', 'tracks', 'videos']))
        except:
            return False
        return True

    def delete_cache(self):
        try:
            if xbmcvfs.exists(FAVORITES_FILE):
                xbmcvfs.delete(FAVORITES_FILE)
                log('Deleted Favorites file.')
        except:
            return False
        return True

    def load_all(self, force_reload=False):
        if not force_reload and self.ids_loaded:
            return self.ids
        if not force_reload:
            self.load_cache()
        if force_reload or not self.ids_loaded:
            Favorites.load_all(self, force_reload=force_reload)
            self.save_cache()
        return self.ids

    def get(self, content_type, limit=9999):
        items = Favorites.get(self, content_type, limit=limit)
        if items:
            self.load_all()
            self.ids[content_type] = sorted(['%s' % item.id for item in items])
            self.save_cache()
        return items

    def add(self, content_type, item_ids):
        ok = Favorites.add(self, content_type, item_ids)
        if ok:
            self.get(content_type)
        return ok

    def remove(self, content_type, item_id):
        ok = Favorites.remove(self, content_type, item_id)
        if ok:
            self.get(content_type)
        return ok

    def isFavoriteArtist(self, artist_id):
        self.load_all()
        return Favorites.isFavoriteArtist(self, artist_id)

    def isFavoriteAlbum(self, album_id):
        self.load_all()
        return Favorites.isFavoriteAlbum(self, album_id)

    def isFavoritePlaylist(self, playlist_id):
        self.load_all()
        return Favorites.isFavoritePlaylist(self, playlist_id)

    def isFavoriteTrack(self, track_id):
        self.load_all()
        return Favorites.isFavoriteTrack(self, track_id)

    def isFavoriteVideo(self, video_id):
        self.load_all()
        return Favorites.isFavoriteVideo(self, video_id)


class TidalUser(User):

    def __init__(self, session, user_id, subscription_type=SubscriptionType.hifi):
        User.__init__(self, session, user_id, subscription_type)
        self.favorites = TidalFavorites(session, user_id)
        self.playlists_loaded = False
        self.playlists_cache = {}

    def load_cache(self):
        try:
            fd = xbmcvfs.File(PLAYLISTS_FILE, 'r')
            self.playlists_cache = eval(fd.read())
            fd.close()
            self.playlists_loaded = True
            log('Loaded %s Playlists from disk.' % len(self.playlists_cache.keys()))
        except:
            self.playlists_loaded = False
            self.playlists_cache = {}
        return self.playlists_loaded

    def save_cache(self):
        try:
            if self.playlists_loaded:
                fd = xbmcvfs.File(PLAYLISTS_FILE, 'w')
                fd.write(repr(self.playlists_cache))
                fd.close()
                log('Saved %s Playlists to disk.' % len(self.playlists_cache.keys()))
        except:
            return False
        return True

    def check_updated_playlist(self, playlist):
        if self.playlists_cache.get(playlist.id, {}).get('lastUpdated', datetime.datetime.fromordinal(1)) == playlist.lastUpdated:
            # Playlist unchanged
            return False
        if playlist.numberOfVideos == 0:
            items = self._session.get_playlist_tracks(playlist)
        else:
            items = self._session.get_playlist_items(playlist)
        album_ids = []
        if ALBUM_PLAYLIST_TAG in playlist.description:
            album_ids = ['%s' % item.album.id for item in items if isinstance(item, TrackItem)]
        # Save Track-IDs into Buffer
        self.playlists_cache.update({playlist.id: {'title': playlist.title,
                                                   'description': playlist.description,
                                                   'lastUpdated': playlist.lastUpdated,
                                                   'ids': ['%s' % item.id for item in items],
                                                   'album_ids': album_ids}})
        return True

    def delete_cache(self):
        try:
            if xbmcvfs.exists(PLAYLISTS_FILE):
                xbmcvfs.delete(PLAYLISTS_FILE)
                log('Deleted Playlists file.')
        except:
            return False
        return True

    def playlists_of_id(self, item_id, album_id=None):
        userpl = {}
        if not self.playlists_loaded:
            self.load_cache()
        if not self.playlists_loaded:
            self.playlists()
        plids = self.playlists_cache.keys()
        for plid in plids:
            if item_id and '%s' % item_id in self.playlists_cache.get(plid).get('ids', []):
                userpl.update({plid: self.playlists_cache.get(plid)})
            if album_id and '%s' % album_id in self.playlists_cache.get(plid).get('album_ids', []):
                userpl.update({plid: self.playlists_cache.get(plid)})
        return userpl

    def playlists(self):
        items = User.playlists(self, offset=0, limit=9999)
        # Refresh the Playlist Cache
        if not self.playlists_loaded:
            self.load_cache()
        buffer_changed = False
        act_ids = [item.id for item in items]
        saved_ids = self.playlists_cache.keys()
        # Remove Deleted Playlists from Cache
        for plid in saved_ids:
            if plid not in act_ids:
                self.playlists_cache.pop(plid)
                buffer_changed = True
        # Update modified Playlists in Cache
        self.playlists_loaded = True
        for item in items:
            if self.check_updated_playlist(item):
                buffer_changed = True
        if buffer_changed:
            self.save_cache()
        return items

    def add_playlist_entries(self, playlist=None, item_ids=[]):
        ok = User.add_playlist_entries(self, playlist=playlist, item_ids=item_ids)
        if ok:
            self.playlists()
        return ok

    def remove_playlist_entry(self, playlist, entry_no=None, item_id=None):
        ok = User.remove_playlist_entry(self, playlist, entry_no=entry_no, item_id=item_id)
        if ok:
            self.playlists()
        return ok

    def delete_playlist(self, playlist_id):
        ok = User.delete_playlist(self, playlist_id)
        if ok:
            self.playlists()
        return ok

    def renamePlaylistDialog(self, playlist):
        dialog = xbmcgui.Dialog()
        title = dialog.input(_T(30233), playlist.title, type=xbmcgui.INPUT_ALPHANUM)
        ok = False
        if title:
            description = dialog.input(_T(30234), playlist.description, type=xbmcgui.INPUT_ALPHANUM)
            ok = self.rename_playlist(playlist, title, description)
        return ok

    def newPlaylistDialog(self):
        dialog = xbmcgui.Dialog()
        title = dialog.input(_T(30233), type=xbmcgui.INPUT_ALPHANUM)
        item = None
        if title:
            description = dialog.input(_T(30234), type=xbmcgui.INPUT_ALPHANUM)
            item = self.create_playlist(title, description)
        return item

    def selectPlaylistDialog(self, headline=None, allowNew=False):
        if not self._session.is_logged_in:
            return None
        xbmc.executebuiltin("ActivateWindow(busydialog)")
        try:
            if not headline:
                headline = _T(30238)
            items = self.playlists()
            dialog = xbmcgui.Dialog()
            item_list = [item.title for item in items]
            if allowNew:
                item_list.append(_T(30237))
        except Exception, e:
            log(str(e), level=xbmc.LOGERROR)
            xbmc.executebuiltin("Dialog.Close(busydialog)")
            return None
        xbmc.executebuiltin("Dialog.Close(busydialog)")
        selected = dialog.select(headline, item_list)
        if selected >= len(items):
            item = self.newPlaylistDialog()
            return item
        elif selected >= 0:
            return items[selected]
        return None


