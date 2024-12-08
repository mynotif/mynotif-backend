"""main URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path
from django.urls.conf import include
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularJSONAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework import routers
from rest_framework.authtoken.views import obtain_auth_token

from nurse import views as nurse_views
from nurse.urls import router as nurse_router
from payment.urls import router as payment_router

from . import views as main_views

router = routers.DefaultRouter()
router.registry.extend(nurse_router.registry)
router.registry.extend(payment_router.registry)

v1_urlpatterns = [
    path("version/", main_views.version, name="version"),
    path("account/register", nurse_views.UserCreate.as_view(), name="register"),
    path("auth/", include(("djoser.urls", "auth"), namespace="auth")),
    path("", include("nurse.urls"), name="nurse"),
    path("payment/", include("payment.urls", namespace="payment")),
    path("api-token-auth/", obtain_auth_token, name="api_token_auth"),
]

v2_urlpatterns = [
    path("account/register", nurse_views.UserCreateV2.as_view(), name="register"),
    path(
        "api-token-auth/",
        main_views.CustomObtainAuthToken.as_view(),
        name="api_token_auth",
    ),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("account/", include("rest_framework.urls")),
    path(
        "swagger.json",
        SpectacularJSONAPIView.as_view(),
        name="schema-json",
    ),
    path(
        "swagger.yaml",
        SpectacularAPIView.as_view(),
        name="schema",
    ),
    path(
        "swagger/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="schema-swagger-ui",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="schema-redoc",
    ),
    path("api/v1/", include((v1_urlpatterns, "v1"), namespace="v1")),
    path("api/v2/", include((v2_urlpatterns, "v2"), namespace="v2")),
]
