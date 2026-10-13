from django.urls import path

from nurse.read_views import PatientReadList

urlpatterns = [path("patients/", PatientReadList.as_view(), name="patients-list")]
