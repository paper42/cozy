import logging
import uuid
import os

from cozy.architecture.event_sender import EventSender
from cozy.control.application_directories import get_cache_dir
from cozy.control.db import get_tracks
import cozy.tools as tools
import cozy.ui

from gi.repository import Gio

from cozy.db.track import Track
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.db.book import Book as BookDB
from cozy.ext import inject
from cozy.model.book import Book
from cozy.model.chapter import Chapter
from cozy.report import reporter

log = logging.getLogger("offline_cache")


class OfflineCache(EventSender):
    """
    This class is responsible for all actions on the offline cache.
    This includes operations like copying to the cache and adding or removing files from
    the cache.
    """
    queue = []
    total_batch_count = 0
    current_batch_count = 0
    current = None
    thread = None
    filecopy_cancel = None
    last_ui_update = 0
    current_book_processing = None

    def __init__(self):
        super().__init__()

        from cozy.media.importer import Importer
        self._importer = inject.instance(Importer)

        self._importer.add_listener(self._on_importer_event)

        self.cache_dir = os.path.join(get_cache_dir(), "offline")
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

        self._start_processing()

        inject.instance(cozy.ui.settings.Settings).add_listener(self.__on_settings_changed)

    def add(self, book: Book):
        """
        Add all tracks of a book to the offline cache and start copying.
        """
        tracks = []
        for chapter in book.chapters:
            file = str(uuid.uuid4())
            tracks.append((chapter.id, file))
        chunks = [tracks[x:x + 500] for x in range(0, len(tracks), 500)]
        for chunk in chunks:
            query = OfflineCacheModel.insert_many(chunk, fields=[OfflineCacheModel.track, OfflineCacheModel.file])
            self.total_batch_count += len(chunk)
            query.execute()

        self._start_processing()

    def remove(self, book: Book):
        """
        Remove all tracks of the given book from the cache.
        """
        self._stop_processing()
        ids = [t.id for t in book.chapters]
        offline_elements = OfflineCacheModel.select().where(OfflineCacheModel.track << ids)

        for element in offline_elements:
            file_path = os.path.join(self.cache_dir, element.file)
            if file_path == self.cache_dir:
                continue

            file = Gio.File.new_for_path(file_path)
            if file.query_exists():
                file.delete()

            for item in self.queue:
                if self.current and item.id == self.current.id:
                    self.filecopy_cancel.cancel()

        OfflineCacheModel.delete().where(OfflineCacheModel.track in ids).execute()
        self.queue = []

        self._start_processing()

    def remove_all_for_storage(self, storage_path):
        """
        """
        for element in OfflineCacheModel.select().join(Track).where(storage_path in Track.file):
            file_path = os.path.join(self.cache_dir, element.file)
            if file_path == self.cache_dir:
                continue

            file = Gio.File.new_for_path(file_path)
            if file.query_exists():
                file.delete()

            if element.track.book.offline == True:
                element.track.book.update(offline=False, downloaded=False).execute()

        OfflineCacheModel.delete().where(storage_path in OfflineCacheModel.track.file).execute()

    def get_cached_path(self, chapter: Chapter):
        """
        """
        query = OfflineCacheModel.select().where(OfflineCacheModel.track == chapter.id, OfflineCacheModel.copied == True)
        if query.count() > 0:
            return os.path.join(self.cache_dir, query.get().file)
        else:
            return None

    def update_cache(self, paths):
        """
        Update the cached version of the given files.
        """
        if OfflineCacheModel.select().count() > 0:
            OfflineCacheModel.update(copied=False).where(
                OfflineCacheModel.track.file in paths).execute()
            self._fill_queue_from_db()

    def delete_cache(self):
        """
        Deletes the entire offline cache files.
        Doesn't delete anything from the cozy.db.
        """
        cache_dir = os.path.join(get_cache_dir(), "offline")

        import shutil
        shutil.rmtree(cache_dir)

    def _stop_processing(self):
        """
        """
        if not self._is_processing() or not self.thread:
            return

        self.filecopy_cancel.cancel()
        self.thread.stop()

    def _start_processing(self):
        """
        """
        if self._is_processing():
            return

        self.thread = tools.StoppableThread(target=self._process_queue)
        self.thread.start()

    def _process_queue(self):
        log.info("Startet processing queue")
        self.filecopy_cancel = Gio.Cancellable()

        self._fill_queue_from_db()
        self.total_batch_count = len(self.queue)
        self.current_batch_count = 0
        if len(self.queue) > 0:
            self.current_book_processing = self.queue[0].track.book.id
            self.emit_event_main_thread("start")

        while len(self.queue) > 0:
            log.info("Processing item")
            self.current_batch_count += 1
            item = self.queue[0]
            if self.thread.stopped():
                break

            new_item = OfflineCacheModel.get(OfflineCacheModel.id == item.id)

            if self.current_book_processing != new_item.track.book.id:
                self.update_book_download_status(
                    BookDB.get(BookDB.id == self.current_book_processing))
                self.current_book_processing = new_item.track.book.id

            if not new_item.copied and os.path.exists(new_item.track.file):
                log.info("Copying item")
                self.emit_event_main_thread("message",
                                            _("Copying") + " " + tools.shorten_string(new_item.track.book.name, 30))
                self.current = new_item

                destination = Gio.File.new_for_path(os.path.join(self.cache_dir, new_item.file))
                source = Gio.File.new_for_path(new_item.track.file)
                flags = Gio.FileCopyFlags.OVERWRITE
                try:
                    copied = source.copy(destination, flags, self.filecopy_cancel, self.__update_copy_status, None)
                except Exception as e:
                    if e.code == Gio.IOErrorEnum.CANCELLED:
                        log.info("Download of book was cancelled.")
                        self.thread.stop()
                        break
                    reporter.exception("offline_cache", e)
                    log.error("Could not copy file to offline cache: " + new_item.track.file)
                    log.error(e)
                    self.queue.remove(item)
                    continue

                if copied:
                    OfflineCacheModel.update(copied=True).where(
                        OfflineCacheModel.id == new_item.id).execute()

            self.queue.remove(item)

        if self.current_book_processing:
            self.update_book_download_status(
                BookDB.get(BookDB.id == self.current_book_processing))

        self.current = None
        self.emit_event_main_thread("finished")

    def update_book_download_status(self, book):
        """
        Updates the downloaded status of a book.
        """
        downloaded = True
        tracks = get_tracks(book)
        offline_tracks = OfflineCacheModel.select().where(OfflineCacheModel.track in tracks)

        if offline_tracks.count() < 1:
            downloaded = False
        else:
            for track in offline_tracks:
                if not track.copied:
                    downloaded = False

        BookDB.update(downloaded=downloaded).where(BookDB.id == book.id).execute()
        if downloaded:
            self.emit_event("book-offline", book)
        else:
            self.emit_event("book-offline-removed", book)

    def _is_processing(self):
        """
        """
        if self.thread:
            return self.thread.is_alive()
        else:
            return False

    def _fill_queue_from_db(self):
        for item in OfflineCacheModel.select().where(OfflineCacheModel.copied == False):
            if not any(item.id == queued.id for queued in self.queue):
                self.queue.append(item)
                self.total_batch_count += 1

    def _on_importer_event(self, event: str, message):
        if event == "new-or-updated-files":
            self.update_cache(message)
            self._start_processing()

    def __update_copy_status(self, current_num_bytes, total_num_bytes, _):
        progress = ((self.current_batch_count - 1) / self.total_batch_count) + (
                (current_num_bytes / total_num_bytes) / self.total_batch_count)
        self.emit_event_main_thread("progress", progress)

    def __on_settings_changed(self, event, message):
        """
        This method reacts to storage settings changes.
        """
        if event == "storage-removed" or event == "external-storage-removed":
            self.remove_all_for_storage(message)
