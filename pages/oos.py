"""pages/oos_hvc.py — wrapper fin, meme pattern que pages/performance.py"""
from views.oos_view import render_oos_hvc_hub


def show_oos_hvc():
    render_oos_hvc_hub()


# ============================================================================
# Dans app.py — remplacer :
# ============================================================================
#
# AVANT
# from pages.listing_pos_oos import show_listing_oos
# from pages.variations_hvc import show_variations_hvc
# ...
# elif selected == "OOS":
#     show_listing_oos()
# elif selected == "Variations HVC":
#     show_variations_hvc()
#
# APRES
# from pages.oos_hvc import show_oos_hvc
# ...
# elif selected == "OOS & Variations HVC":
#     show_oos_hvc()
#
# Dans option_menu, remplacer les deux entrees "OOS" et "Variations HVC"
# par une seule "OOS & Variations HVC" avec une icone (ex: "bar-chart").
# ============================================================================