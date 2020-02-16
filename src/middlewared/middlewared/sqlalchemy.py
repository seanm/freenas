import datetime
import json

from sqlalchemy import (
    Table, Column as _Column, ForeignKey, Index,
    Boolean, CHAR, DateTime, Integer, SmallInteger, String, Text,
)  # noqa
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship  # noqa
from sqlalchemy.types import UserDefinedType

from middlewared.plugins.pwenc import encrypt, decrypt


Model = declarative_base()
Model.metadata.naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}


class Column(_Column):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("nullable", False)
        super().__init__(*args, **kwargs)


class EncryptedText(UserDefinedType):
    def get_col_spec(self, **kw):
        return "TEXT"

    def _bind_processor(self, value):
        return encrypt(value) if value else ''

    def bind_processor(self, dialect):
        return self._bind_processor

    def _result_processor(self, value):
        return decrypt(value) if value else ''

    def result_processor(self, dialect, coltype):
        return self._result_processor


class JSON(UserDefinedType):
    def __init__(self, type=dict, encrypted=False):
        self.type = type
        self.encrypted = encrypted

    def get_col_spec(self, **kw):
        return "TEXT"

    def _bind_processor(self, value):
        result = json.dumps(value or self.type())
        if self.encrypted:
            result = encrypt(result)
        return result

    def bind_processor(self, dialect):
        return self._bind_processor

    def _result_processor(self, value):
        try:
            if self.encrypted:
                value = decrypt(value, _raise=True)
            return json.loads(value)
        except Exception:
            return self.type()

    def result_processor(self, dialect, coltype):
        return self._result_processor


class MultiSelectField(UserDefinedType):
    def get_col_spec(self, **kw):
        return "TEXT"

    def _bind_processor(self, value):
        if value is None:
            return None

        return ",".join(value)

    def bind_processor(self, dialect):
        return self._bind_processor

    def _result_processor(self, value):
        if value:
            try:
                return value.split(",")
            except Exception:
                pass

        return []

    def result_processor(self, dialect, coltype):
        return self._result_processor


class Time(UserDefinedType):
    def get_col_spec(self, **kw):
        return "TIME"

    def _bind_processor(self, value):
        if value is None:
            return None

        return value.isoformat()

    def bind_processor(self, dialect):
        return self._bind_processor

    def _result_processor(self, value):
        try:
            return datetime.time(*map(int, value.split(":")))
        except Exception:
            return datetime.time()

    def result_processor(self, dialect, coltype):
        return self._result_processor
