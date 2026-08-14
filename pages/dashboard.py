"""Dashboard page wrapper.

Rendering lives in ``views.dashboard_view`` and business orchestration lives in
``controllers.dashboard_controller``.
"""

from controllers.dashboard_controller import build_dashboard_context, load_filter_options
from views.dashboard_view import render_dashboard, render_dashboard_filters


def show_dashboard():
    options = load_filter_options()
    filters = render_dashboard_filters(options)
    context = build_dashboard_context(filters)
    render_dashboard(context)
