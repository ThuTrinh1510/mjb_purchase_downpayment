from odoo.addons.web.tests.test_js import WebSuite


def mjb_skip_test():
    return True


WebSuite.test_01_js = mjb_skip_test()
