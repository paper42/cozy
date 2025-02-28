import pytest

import cozy.ext.inject as inject
from peewee import SqliteDatabase


@pytest.fixture(autouse=True)
def setup_inject(peewee_database):
    inject.clear_and_configure(lambda binder: binder.bind(SqliteDatabase, peewee_database))
    yield
    inject.clear()


def test_storage_locations_contains_every_storage_location_from_db(peewee_database):
    from cozy.model.settings import Settings
    from cozy.db.storage import Storage

    settings = Settings()
    storage_locations = Storage.select()

    assert len(settings.storage_locations) == len(storage_locations)
    assert [storage.path for storage in settings.storage_locations] == [storage.path for storage in storage_locations]
    assert [storage.location_type for storage in settings.storage_locations] == [storage.location_type for storage in
                                                                                 storage_locations]
    assert [storage.default for storage in settings.storage_locations] == [storage.default for storage in
                                                                           storage_locations]
    assert [storage.external for storage in settings.storage_locations] == [storage.external for storage in
                                                                            storage_locations]


def test_external_storage_locations_contain_only_external_storages(peewee_database):
    from cozy.model.settings import Settings
    from cozy.db.storage import Storage

    settings = Settings()
    storage_locations = Storage.select().where(Storage.external == True)

    assert len(settings.external_storage_locations) == len(storage_locations)
    assert all([storage.external for storage in settings.external_storage_locations])


def test_last_played_book_returns_correct_value(peewee_database):
    from cozy.model.settings import Settings
    from cozy.db.book import Book

    settings = Settings()

    assert settings.last_played_book == Book.get()


def test_setting_last_played_book_to_none_updates_in_settings_object_and_database(peewee_database):
    from cozy.model.settings import Settings
    from cozy.db.settings import Settings as SettingsModel

    settings = Settings()
    settings.last_played_book = None

    assert settings.last_played_book == None
    assert SettingsModel.get().last_played_book == None
