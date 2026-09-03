import os
import logging
from time import time, sleep
from functools import wraps
from typing import Dict, Optional

import pyinotify

from ulauncher.utils.decorator.run_async import run_async
from ulauncher.utils.desktop.reader import find_desktop_files, read_desktop_file, filter_app, find_apps_cached
from ulauncher.utils.Settings import Settings
from ulauncher.search.apps.AppDb import AppDb
from ulauncher.config import DESKTOP_DIRS

logger = logging.getLogger(__name__)


def _is_desktop_file(pathname: str) -> bool:
    """
    :returns: True if `pathname` looks like a `*.desktop` file
    """
    return os.path.splitext(pathname)[1] == '.desktop'


def _only_desktop_files(func):
    """
    Decorator for pyinotify.ProcessEvent
    Triggers event handler only for *.desktop files
    """

    @wraps(func)
    def decorator_func(self, event, *args, **kwargs):
        if _is_desktop_file(event.pathname):
            return func(self, event, *args, **kwargs)
        return None

    return decorator_func


DeferredFiles = Dict[str, float]

settings = Settings.get_instance()
disable_desktop_filters = settings.get_property('disable-desktop-filters')


class AppNotifyEventHandler(pyinotify.ProcessEvent):
    RETRY_INTERVAL = 2  # seconds
    RETRY_TIME_SPAN = (5, 30)  # make an attempt to process desktop file within 5 to 30 seconds after event came in
    # otherwise application icon or .desktop file itself may not be ready

    class InvalidDesktopFile(IOError):
        pass

    class SkipDesktopFile(Exception):
        pass

    def __init__(self, db):
        super().__init__()
        self.__db = db  # type: AppDb

        # key is a file path, value is an addition time
        self._deferred_files = {}  # type: DeferredFiles
        # set by watch_desktop_dirs() so a desktop dir can be watched again if it reappears
        self._watch_manager: Optional[pyinotify.WatchManager] = None
        self._watch_mask = 0
        self._init_worker()

    @run_async(daemon=True)
    def _init_worker(self) -> None:
        """
        Add files to the DB with some delay,
        otherwise .desktop file may not be ready while application is being installed
        """
        while True:
            for pathname, start_time in list(self._deferred_files.items()):
                time_passed = time() - start_time
                if time_passed < self.RETRY_TIME_SPAN[0]:
                    # skip this file for now
                    continue

                if time_passed > self.RETRY_TIME_SPAN[1]:
                    # give up on file after time limit
                    if pathname in self._deferred_files:
                        del self._deferred_files[pathname]

                try:
                    self._add_file_sync(pathname)
                except self.InvalidDesktopFile:
                    # retry
                    pass
                except self.SkipDesktopFile:
                    # skip adding, and break the loop
                    if pathname in self._deferred_files:
                        del self._deferred_files[pathname]
                    break
                # pylint: disable=broad-except
                except Exception as e:
                    # give up on unexpected exception
                    logger.warning("Unexpected exception: %s", e)
                    if pathname in self._deferred_files:
                        del self._deferred_files[pathname]
                else:
                    # success
                    if pathname in self._deferred_files:
                        del self._deferred_files[pathname]

            sleep(self.RETRY_INTERVAL)

    def add_file_deferred(self, pathname: str) -> None:
        """
        Add .desktop file to DB a little bit later
        """
        self._deferred_files[pathname] = time()

    def _add_file_sync(self, pathname: str) -> None:
        """
        Add .desktop file to DB

        Raises self.InvalidDesktopFile if failed to add an app
        """

        # get filename of the desktop file (i.e chromium.desktop)
        file_name = os.path.basename(pathname)
        # search for desktop file in all of DESKTOP_DIRS
        pathnames_in_xdg_dirs = list(find_desktop_files(DESKTOP_DIRS, file_name))
        # if the pathname is not found in the desktop files it is overridden
        # with a file of the same name in a different XDG directory, so skip
        # trying to add this desktop file
        if pathname not in pathnames_in_xdg_dirs:
            logger.warning('Skipping adding %s to DB -> desktop file overridden in a different XDG directory', pathname)
            raise self.SkipDesktopFile(pathname)

        try:
            app = read_desktop_file(pathname)
            if filter_app(app, disable_desktop_filters):
                self.__db.put_app(app)
                logger.info('New app was added "%s" (%s)', app.get_name(), app.get_filename())
            else:
                raise self.InvalidDesktopFile(pathname)
        except Exception as e:
            logger.warning('Cannot add %s to DB -> %s', pathname, e)
            raise self.InvalidDesktopFile(pathname)

    def _remove_file(self, pathname: str) -> None:
        """
        Remove .desktop file from DB
        :param str pathname:
        """
        self.__db.remove_by_path(pathname)
        logger.info('App was removed (%s)', pathname)

    def watch_desktop_dirs(self, watch_manager, mask: int) -> None:
        """
        Watch every desktop dir that exists, and the parent of every desktop dir we know of.

        The parent watches are what make this survive a desktop dir being created after
        startup, or deleted and recreated while we run. inotify watches an inode rather than
        a path, so a deleted dir takes its watch with it and the replacement is invisible
        until a new watch is added.
        """
        self._watch_manager = watch_manager
        self._watch_mask = mask

        existing_dirs = [path for path in DESKTOP_DIRS if os.path.isdir(path)]
        if existing_dirs:
            watch_manager.add_watch(existing_dirs, mask, rec=True, auto_add=True)

        # Not recursive, and creation events only -- all we need to hear about is the
        # desktop dir itself reappearing.
        parent_dirs = sorted({os.path.dirname(path) for path in DESKTOP_DIRS
                              if os.path.isdir(os.path.dirname(path))})
        if parent_dirs:
            # pylint: disable=no-member
            watch_manager.add_watch(parent_dirs, pyinotify.IN_CREATE | pyinotify.IN_MOVED_TO)

    def _watch_desktop_dir_again(self, pathname: str) -> None:
        """
        Give a desktop dir that just appeared a fresh watch, and index what is already in it
        """
        if self._watch_manager is None or pathname not in DESKTOP_DIRS:
            return

        logger.info('Desktop dir appeared, watching it again (%s)', pathname)
        self._watch_manager.add_watch(pathname, self._watch_mask, rec=True, auto_add=True)

        # Files can land between the dir being created and the watch being added, so take
        # what is there now instead of relying on events alone.
        for pathname_in_dir in find_desktop_files([pathname]):
            self.add_file_deferred(pathname_in_dir)

    def process_IN_CREATE(self, event):
        if event.dir:
            self._watch_desktop_dir_again(event.pathname)
        elif _is_desktop_file(event.pathname):
            self.add_file_deferred(event.pathname)

    @_only_desktop_files
    def process_IN_DELETE(self, event):
        self._remove_file(event.pathname)

    def process_IN_DELETE_SELF(self, event):
        # Logged because the watch is now gone: without this the app silently disappears
        # from results and nothing in the log says why.
        if event.pathname in DESKTOP_DIRS:
            logger.info('Desktop dir was removed, waiting for it to reappear (%s)', event.pathname)

    @_only_desktop_files
    def process_IN_MODIFY(self, event):
        self.add_file_deferred(event.pathname)

    @_only_desktop_files
    def process_IN_MOVED_FROM(self, event):
        self._remove_file(event.pathname)

    def process_IN_MOVED_TO(self, event):
        if event.dir:
            self._watch_desktop_dir_again(event.pathname)
        elif _is_desktop_file(event.pathname):
            self.add_file_deferred(event.pathname)


@run_async(daemon=True)
def start():
    """
    Add all known .desktop files to the DB and start inotify watcher
    """

    db = AppDb.get_instance()
    t0 = time()
    logger.info('Started scanning desktop dirs')
    for app in find_apps_cached(None, disable_desktop_filters):
        db.put_app(app)
    logger.info('Scanned desktop dirs in %.2f seconds', (time() - t0))

    wm = pyinotify.WatchManager()
    handler = AppNotifyEventHandler(db)
    notifier = pyinotify.ThreadedNotifier(wm, handler)
    notifier.setDaemon(True)
    logger.debug('Start watching desktop files...')
    notifier.start()
    # pylint: disable=no-member
    mask = pyinotify.IN_CREATE | pyinotify.IN_DELETE | pyinotify.IN_MODIFY | \
        pyinotify.IN_MOVED_FROM | pyinotify.IN_MOVED_TO | pyinotify.IN_DELETE_SELF
    handler.watch_desktop_dirs(wm, mask)
