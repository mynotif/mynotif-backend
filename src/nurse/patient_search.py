"""Exact contract normalization and SQL-only patient search."""

import unicodedata

from django.db import connections, models, router
from django.db.models.functions import StrIndex

SEARCH_FIELDS = ("firstname", "lastname", "city", "phone")
SEARCH_COLUMNS = tuple(f"search_{field}" for field in SEARCH_FIELDS)


def normalize_patient_search(value):
    if not isinstance(value, str):
        raise ValueError("Patient search sources must be literal strings.")
    return "".join(
        char
        for char in unicodedata.normalize("NFD", value)
        if not unicodedata.category(char).startswith("M")
    ).casefold()


def reject_search_columns(fields):
    if set(fields).intersection(SEARCH_COLUMNS):
        raise ValueError("Patient search columns are derived and cannot be written.")


def prepare_search_columns(obj, fields=SEARCH_FIELDS):
    for field in SEARCH_FIELDS:
        if field in fields:
            setattr(
                obj, f"search_{field}", normalize_patient_search(getattr(obj, field))
            )


class PatientSearchSaveMixin:
    def save(
        self, force_insert=False, force_update=False, using=None, update_fields=None
    ):
        using = using or router.db_for_write(type(self), instance=self)
        if update_fields is not None:
            update_fields = set(update_fields)
            reject_search_columns(update_fields)
        elif (
            self.get_deferred_fields() and not force_insert and self._state.db == using
        ):
            # Match Django's deferred-save behavior, without loading the other
            # sources or persisting a standalone derived column accidentally.
            update_fields = {
                field.attname
                for field in self._meta.concrete_fields
                if not field.primary_key
                and not field.generated
                and field.attname not in self.get_deferred_fields()
                and field.attname not in SEARCH_COLUMNS
            }
        if update_fields is None:
            prepare_search_columns(self)
        elif update_fields:
            prepare_search_columns(self, update_fields)
            update_fields.update(
                f"search_{field}" for field in SEARCH_FIELDS if field in update_fields
            )
        return super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )


class PatientQuerySet(models.QuerySet):
    def search_normalized(self, search):
        queryset = self
        for index, term in enumerate(normalize_patient_search(search).split()):
            # PostgreSQL text cannot contain NUL; do not bind an unsupported
            # parameter. Such a literal term cannot match any stored source.
            if "\x00" in term and connections[self.db].vendor == "postgresql":
                return queryset.none()
            # Database substring position gives literal matching, including NUL
            # on SQLite (whose LIKE operator truncates NUL-containing patterns).
            aliases = {
                f"m03_term_{index}_{column}": StrIndex(column, models.Value(term))
                for column in SEARCH_COLUMNS
            }
            condition = models.Q()
            for alias in aliases:
                condition |= models.Q(**{f"{alias}__gt": 0})
            queryset = queryset.alias(**aliases).filter(condition)
        return queryset

    def update(self, **kwargs):
        reject_search_columns(kwargs)
        for field in SEARCH_FIELDS:
            if field in kwargs:
                kwargs[f"search_{field}"] = normalize_patient_search(kwargs[field])
        return super().update(**kwargs)

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        objs = list(objs)
        if update_fields is not None:
            update_fields = list(update_fields)
            reject_search_columns(update_fields)
            update_fields += [
                f"search_{field}" for field in SEARCH_FIELDS if field in update_fields
            ]
        for obj in objs:
            prepare_search_columns(obj)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )

    def bulk_update(self, objs, fields, batch_size=None):
        fields = list(fields)
        reject_search_columns(fields)
        objs = tuple(objs)
        for obj in objs:
            prepare_search_columns(obj, fields)
        fields += [f"search_{field}" for field in SEARCH_FIELDS if field in fields]
        # Django builds CASE expressions internally. Use its unmodified queryset
        # only after all user input has passed the literal-source guards above.
        queryset = models.QuerySet(
            model=self.model,
            query=self.query.chain(),
            using=self.db,
            hints=self._hints,
        )
        return queryset.bulk_update(objs, fields, batch_size=batch_size)
