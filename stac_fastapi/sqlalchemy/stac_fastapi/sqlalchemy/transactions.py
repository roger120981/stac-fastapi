"""transactions extension client."""

import json
import logging
from typing import Dict, Optional, Type

import attr

# TODO: This import should come from `backend` module
from stac_fastapi.extensions.third_party.bulk_transactions import (
    BaseBulkTransactionsClient,
)
from stac_fastapi.sqlalchemy import serializers
from stac_fastapi.sqlalchemy.models import database, schemas
from stac_fastapi.sqlalchemy.session import Session
from stac_fastapi.types.core import BaseTransactionsClient
from stac_fastapi.types.errors import NotFoundError
from stac_fastapi.types.stac import Collection, Item

logger = logging.getLogger(__name__)


@attr.s
class TransactionsClient(BaseTransactionsClient):
    """Transactions extension specific CRUD operations."""

    session: Session = attr.ib(default=attr.Factory(Session.create_from_env))
    collection_table: Type[database.Collection] = attr.ib(default=database.Collection)
    item_table: Type[database.Item] = attr.ib(default=database.Item)
    item_serializer: Type[serializers.Serializer] = attr.ib(
        default=serializers.ItemSerializer
    )
    collection_serializer: Type[serializers.Serializer] = attr.ib(
        default=serializers.CollectionSerializer
    )

    def create_item(self, model: schemas.Item, **kwargs) -> Item:
        """Create item."""
        base_url = str(kwargs["request"].base_url)
        data = self.item_serializer.stac_to_db(model.dict(exclude_none=True))
        with self.session.writer.context_session() as session:
            session.add(data)
            return self.item_serializer.db_to_stac(data, base_url)

    def create_collection(self, model: schemas.Collection, **kwargs) -> Collection:
        """Create collection."""
        base_url = str(kwargs["request"].base_url)
        data = self.collection_serializer.stac_to_db(model.dict(exclude_none=True))
        with self.session.writer.context_session() as session:
            session.add(data)
            return self.collection_serializer.db_to_stac(data, base_url=base_url)

    def update_item(self, model: schemas.Item, **kwargs) -> Item:
        """Update item."""
        base_url = str(kwargs["request"].base_url)
        with self.session.reader.context_session() as session:
            query = session.query(self.item_table).filter(
                self.item_table.id == model.id
            )
            if not query.scalar():
                raise NotFoundError(f"Item {model.id} not found")
            # SQLAlchemy orm updates don't seem to like geoalchemy types
            db_model = self.item_serializer.stac_to_db(
                model.dict(exclude_none=True), exclude_geometry=True
            )
            query.update(self.item_serializer.row_to_dict(db_model))

            # TODO: Fix this by allowing geoetry updates (there is a PR out to do this)
            stac_item = self.item_serializer.db_to_stac(db_model, base_url)
            stac_item["geometry"] = model.geometry.dict()
            return stac_item

    def update_collection(self, model: schemas.Collection, **kwargs) -> Collection:
        """Update collection."""
        base_url = str(kwargs["request"].base_url)
        with self.session.reader.context_session() as session:
            query = session.query(self.collection_table).filter(
                self.collection_table.id == model.id
            )
            if not query.scalar():
                raise NotFoundError(f"Item {model.id} not found")

            # SQLAlchemy orm updates don't seem to like geoalchemy types
            db_model = self.collection_serializer.stac_to_db(
                model.dict(exclude_none=True)
            )
            query.update(self.collection_serializer.row_to_dict(db_model))

            return self.collection_serializer.db_to_stac(db_model, base_url)

    def delete_item(self, item_id: str, collection_id: str, **kwargs) -> Item:
        """Delete item."""
        base_url = str(kwargs["request"].base_url)
        with self.session.writer.context_session() as session:
            query = session.query(self.item_table).filter(self.item_table.id == item_id)
            data = query.first()
            if not data:
                raise NotFoundError(f"Item {id} not found")
            query.delete()
            return self.item_serializer.db_to_stac(data, base_url=base_url)

    def delete_collection(self, id: str, **kwargs) -> Collection:
        """Delete collection."""
        base_url = str(kwargs["request"].base_url)
        with self.session.writer.context_session() as session:
            query = session.query(self.collection_table).filter(
                self.collection_table.id == id
            )
            data = query.first()
            if not data:
                raise NotFoundError(f"Collection {id} not found")
            query.delete()
            return self.collection_serializer.db_to_stac(data, base_url=base_url)


@attr.s
class BulkTransactionsClient(BaseBulkTransactionsClient):
    """Postgres bulk transactions."""

    session: Session = attr.ib(default=attr.Factory(Session.create_from_env))
    debug: bool = attr.ib(default=False)

    def __attrs_post_init__(self):
        """Create sqlalchemy engine."""
        self.engine = self.session.writer.cached_engine

    @staticmethod
    def _preprocess_item(item: schemas.Item) -> Dict:
        """Preprocess items to match data model.

        # TODO: dedup with GetterDict logic (ref #58)
        """
        item = item.dict(exclude_none=True)
        item["geometry"] = json.dumps(item["geometry"])
        item["collection_id"] = item.pop("collection")
        item["datetime"] = item["properties"].pop("datetime")
        return item

    def bulk_item_insert(
        self, items: schemas.Items, chunk_size: Optional[int] = None, **kwargs
    ) -> str:
        """Bulk item insertion using sqlalchemy core.

        https://docs.sqlalchemy.org/en/13/faq/performance.html#i-m-inserting-400-000-rows-with-the-orm-and-it-s-really-slow
        """
        # Use items.items because schemas.Items is a model with an items key
        processed_items = [self._preprocess_item(item) for item in items.items]
        return_msg = f"Successfully added {len(processed_items)} items."
        if chunk_size:
            for chunk in self._chunks(processed_items, chunk_size):
                self.engine.execute(database.Item.__table__.insert(), chunk)
            return return_msg

        self.engine.execute(database.Item.__table__.insert(), processed_items)
        return return_msg
