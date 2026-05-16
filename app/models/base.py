from peewee import Model

from app.db import db_proxy


class BaseModel(Model):
    class Meta:
        database = db_proxy
