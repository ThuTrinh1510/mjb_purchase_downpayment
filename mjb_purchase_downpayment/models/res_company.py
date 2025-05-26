# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    purchase_down_payment_product_id = fields.Many2one(
        comodel_name='product.product',
        string="Deposit Product",
        domain=[
            ('type', '=', 'service'),
            ('purchase_method', '=', 'purchase'),
        ],
        help="Default product used for down payments",
        check_company=True,
    )