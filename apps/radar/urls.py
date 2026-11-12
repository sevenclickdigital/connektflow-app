from django.urls import path

from . import views

app_name = "radar"

urlpatterns = [
    path("", views.index, name="index"),
    path("shopee/", views.shopee, name="shopee"),
    path("shopee/favorites/", views.shopee_favorites, name="shopee_favorites"),
    path("shopee/offer/favorite/", views.offer_favorite, name="offer_favorite"),
    path("shopee/offer/arts/", views.offer_art_request, name="offer_art_request"),
    path("shopee/offer/post/", views.offer_post, name="offer_post"),
    path("connect/shopee/", views.connect_shopee, name="connect_shopee"),
    path("<uuid:connection_id>/disconnect/", views.disconnect, name="disconnect"),
]
