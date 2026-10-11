from django.urls import path

from main.session_views import (
    SessionCollectionView,
    SessionCurrentView,
    SessionRefreshView,
    SessionRevokeView,
)

app_name = "sessions"
urlpatterns = [
    path("", SessionCollectionView.as_view(), name="collection"),
    path("refresh/", SessionRefreshView.as_view(), name="refresh"),
    path("current/", SessionCurrentView.as_view(), name="current"),
    path("<uuid:session_id>/", SessionRevokeView.as_view(), name="detail"),
]
