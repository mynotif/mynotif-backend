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

handler400 = "rest_framework.exceptions.bad_request"
handler500 = "rest_framework.exceptions.server_error"

router = routers.DefaultRouter()
router.registry.extend(nurse_router.registry)
router.registry.extend(payment_router.registry)

v1_urlpatterns = [
    path("version/", main_views.version, name="version"),
    path("account/register", nurse_views.UserCreate.as_view(), name="register"),
    path("auth/", include(("main.account_urls", "auth"), namespace="auth")),
    path("", include("nurse.urls"), name="nurse"),
    path("payment/", include("payment.urls", namespace="payment")),
    path("api-token-auth/", obtain_auth_token, name="api_token_auth"),
    path("error-400/", main_views.Error400View.as_view(), name="error_400"),
    path("api-error-400/", main_views.Error400APIView.as_view(), name="api_error_400"),
    path("error-404/", main_views.Error404View.as_view(), name="error_404"),
    path("api-error-404/", main_views.Error404APIView.as_view(), name="api_error_404"),
    path("error-500/", main_views.Error500View.as_view(), name="error_500"),
    path("api-error-500/", main_views.Error500APIView.as_view(), name="api_error_500"),
]

v2_urlpatterns = [
    path("", include(("nurse.read_urls", "reads"), namespace="reads")),
    path("account/register", nurse_views.UserCreateV2.as_view(), name="register"),
    path(
        "auth/sessions/",
        include(("main.session_urls", "sessions"), namespace="sessions"),
    ),
    path(
        "api-token-auth/",
        main_views.CustomObtainAuthToken.as_view(),
        name="api_token_auth",
    ),
]

v3_urlpatterns = [
    path("", include(("nurse.document_import_urls", "document_imports"))),
    path("", include(("nurse.document_reads_urls", "document_reads"))),
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
    path("api/v3/", include((v3_urlpatterns, "v3"), namespace="v3")),
]
