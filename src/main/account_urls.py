from rest_framework.routers import DefaultRouter

from main.account_views import AccountViewSet

router = DefaultRouter()
router.register("users", AccountViewSet)

urlpatterns = router.urls
