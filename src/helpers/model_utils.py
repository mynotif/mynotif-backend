from django.core.exceptions import BadRequest


def get_object_or_exception(model, exception_class, **kwargs):
    """
    Retrieve an object from the database or
    raise a specified exception if the object does not exist.

    Parameters:
    model (django.db.models.Model):
    The Django model class to query.
    exception_class (Exception):
    The exception class to raise if the object is not found.
    **kwargs: Keyword arguments to pass to the model's `get()` method.

    Returns:
    The retrieved object.

    Raises:
    exception_class: If the object does not exist.
    """
    try:
        return model.objects.get(**kwargs)
    except model.DoesNotExist:
        raise exception_class(f"{model.__name__} does not exist.")


def get_object_or_400(model, **kwargs):
    """
    Retrieve an object from the database or
    raise a BadRequest exception if the object does not exist.

    Parameters:
    model (django.db.models.Model): The Django model class to query.
    **kwargs: Keyword arguments to pass to the model's `get()` method.

    Returns:
    The retrieved object.

    Raises:
    BadRequest: If the object does not exist.
    """
    return get_object_or_exception(model, BadRequest, **kwargs)
