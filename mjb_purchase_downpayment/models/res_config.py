# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api
from ast import literal_eval


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    po_deposit_default_product_id = fields.Many2one(
        related='company_id.purchase_down_payment_product_id',
        readonly=False,
    )