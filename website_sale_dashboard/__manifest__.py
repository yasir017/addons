{
    'name': 'Website Sales Dashboard',
    'category': 'Hidden',
    'sequence': 55,
    'summary': 'Get a new dashboard view in the Website App',
    'version': '1.0',
    'description': """
This module adds a new dashboard view in the Website application.
This new type of view contains some basic statistics, a graph, and a pivot subview that allow you to get a quick overview of your online sales.
It also provides new tools to analyse your data.
    """,
    'depends': ['website_sale', 'web_dashboard'],
    'data': [
        'views/dashboard_view.xml',
        'views/res_config_settings_views.xml'
    ],
    'auto_install': ['website_sale'],
    'license': 'OEEL-1',
    'assets': {
        'web.assets_backend': [
            'website_sale_dashboard/static/src/js/website_sale_dashboard_view.js',
        ],
        'web.assets_backend_legacy_lazy': [
            'website_sale_dashboard/static/src/js/website_sale_dashboard_legacy.js',
            'website_sale_dashboard/static/src/js/website_sale_dashboard_controller.js',
        ],
        'web.qunit_suite_tests': [
            'website_sale_dashboard/static/tests/**/*',
        ],
        'web.assets_qweb': [
            'website_sale_dashboard/static/src/xml/*.xml',
        ],
    }
}
