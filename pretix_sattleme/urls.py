from django.urls import include, re_path

from .views import PayView, StatusView

event_patterns = [
    re_path(
        r"^sattleme/(?P<order>[^/]+)/(?P<hash>[0-9a-f]{64})/(?P<payment>[0-9]+)/",
        include(
            [
                re_path(r"^$", PayView.as_view(), name="pay"),
                re_path(r"^status/$", StatusView.as_view(), name="status"),
            ]
        ),
    ),
]
