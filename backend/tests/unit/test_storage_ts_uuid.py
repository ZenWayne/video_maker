import re
from app.services import storage


def test_ts_uuid_name_format():
    name = storage.ts_uuid_name()
    assert re.fullmatch(r"\d+_[0-9a-f]{8}\.png", name), name


def test_ts_uuid_name_unique():
    assert storage.ts_uuid_name() != storage.ts_uuid_name()


def test_ts_uuid_name_custom_ext():
    assert storage.ts_uuid_name(".jpg").endswith(".jpg")
