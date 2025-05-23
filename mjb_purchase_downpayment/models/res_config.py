# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api
from ast import literal_eval


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    mjb_deposit_product_id = fields.Many2one(
        'product.product',
        string="Down Payments",
        domain="[('type', '=', 'service'), ('purchase_method', '=', 'purchase')]",
        help='Default product used for payment advances'
    )

    @api.model
    def get_values(self):
        res = super(ResConfigSettings, self).get_values()
        ICPSudo = self.env['ir.config_parameter'].sudo()
        mjb_deposit_product_id = literal_eval(ICPSudo.get_param(
            'mjb_purchase_downpayment.mjb_deposit_product_id',
            default='False'))
        if mjb_deposit_product_id and not self.env['product.product'].browse(
                mjb_deposit_product_id).exists():
            mjb_deposit_product_id = False
        res.update(
            mjb_deposit_product_id=mjb_deposit_product_id,
        )
        return res

    def set_values(self):
        res = super(ResConfigSettings, self).set_values()
        ICPSudo = self.env['ir.config_parameter'].sudo()
        ICPSudo.set_param(
            "mjb_purchase_downpayment.mjb_deposit_product_id",
            self.mjb_deposit_product_id.id)
        return res
