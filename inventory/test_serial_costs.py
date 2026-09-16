from decimal import Decimal
from datetime import date
from django.test import TestCase, SimpleTestCase
from rest_framework.exceptions import ValidationError
from components.models import Component
from vendors.models import Vendor
from procurement.models import PurchaseOrder, PurchaseOrderItem
from inward.models import InwardEntry, InwardLineItem
from outward.models import OutwardEntry
from inventory.models import Inventory, SerialPurchaseCost
from inventory.cost_math import allocate, unit_allocations
from inventory.costing import record_inward_costs, inward_cost_details, record_inventory_costs
from inward.serializers import InwardEntrySerializer
from inventory.serializers import InventorySerializer
from outward.serializers import OutwardEntrySerializer


class MoneyAllocationTests(SimpleTestCase):
    def test_eight_serials_preserve_180_60(self):
        values = allocate('180.60',[1]*8)
        self.assertEqual(values,[Decimal('22.58')]*4+[Decimal('22.57')]*4)
        self.assertEqual(sum(values),Decimal('180.60'))

    def test_negative_roundoff_weighting(self):
        self.assertEqual(allocate('-.05',[1,3]),[Decimal('-.01'),Decimal('-.04')])

    def test_parts_and_final_cost_reconcile(self):
        units=unit_allocations(dict(basic_amount='160',gst_amount='20.60',grand_total='180.60'),8)
        for unit in units:
            subtotal=Decimal(unit['basic_amount'])+Decimal(unit['gst_amount'])+Decimal(unit['rounding_adjustment'])
            self.assertEqual(subtotal,Decimal(unit['allocated_cost']))


class SerialCostFlowTests(TestCase):
    def setUp(self):
        self.component=Component.objects.create(component_id='CMP-COST',name='Wings',category='AIRFRAMES')
        self.vendor=Vendor.objects.create(name='Cost Supplier')
        self.po=PurchaseOrder.objects.create(po_number='COST/26-27',vendor_name=self.vendor.name,round_off='0.60')
        self.item=PurchaseOrderItem.objects.create(purchase_order=self.po,component=self.component,quantity=8,unit_price='20',gst_percentage='12.50')

    def receipt(self,code='INW-COST',qty=8,po=True):
        return InwardEntry.objects.create(code=code,vendor=self.vendor,purchase_order=self.po if po else None,
            component=self.component,quantity_received=qty,received_date=date(2026,9,4))

    def qc(self,receipt,passed,failed=()):
        receipt.qc_passed_rows=[{'id':i,'serial_number':f'SERIAL-{receipt.pk}-{i}','qty':1,'remarks':'OK'} for i in passed]
        receipt.qc_failed_rows=[{'id':i,'serial_number':f'SERIAL-{receipt.pk}-{i}','qty':1,'remarks':'Failed'} for i in failed]
        receipt.save()
        record_inward_costs(receipt)
        return [row['serial_number'] for row in receipt.qc_passed_rows]

    def test_qc_pass_failure_and_repeat_keep_original_cost(self):
        receipt=self.receipt()
        self.qc(receipt,[1,2,3,4],[5,6,7,8])
        self.assertEqual(SerialPurchaseCost.objects.count(),8)
        self.assertEqual(sum(SerialPurchaseCost.objects.values_list('allocated_cost',flat=True)),Decimal('180.60'))
        self.item.unit_price=99;self.item.save()
        record_inward_costs(receipt)
        self.assertEqual(sum(SerialPurchaseCost.objects.values_list('allocated_cost',flat=True)),Decimal('180.60'))
        detail=InwardEntrySerializer(receipt).data['cost_details'][0]
        self.assertTrue(detail['cost_complete'])
        self.assertEqual(len([row for row in detail['serials'] if row['status']=='QC failed']),4)

    def test_partial_qc_uses_stable_row_slots(self):
        receipt=self.receipt()
        self.qc(receipt,[7],[2])
        first=dict(SerialPurchaseCost.objects.values_list('serial_number','allocated_cost'))
        self.qc(receipt,[1,3,4,7],[2,5,6,8])
        costs=dict(SerialPurchaseCost.objects.values_list('serial_number','allocated_cost'))
        for serial,value in first.items():self.assertEqual(costs[serial],value)
        self.assertEqual(sum(costs.values()),Decimal('180.60'))

    def test_partial_receipts_do_not_duplicate_full_po_value(self):
        first=self.receipt('INW-A',3);second=self.receipt('INW-B',5)
        self.qc(first,[1,2,3]);self.qc(second,[1,2,3,4,5])
        self.assertEqual(sum(SerialPurchaseCost.objects.values_list('allocated_cost',flat=True)),Decimal('180.60'))

    def test_multiple_components_receive_own_prices(self):
        other=Component.objects.create(component_id='CMP-OTHER',name='Motor',category='ELECTRICALS')
        PurchaseOrderItem.objects.create(purchase_order=self.po,component=other,quantity=2,unit_price='100',gst_percentage=0)
        receipt=self.receipt();self.qc(receipt,list(range(1,9)))
        other_receipt=InwardEntry.objects.create(code='INW-MOTOR',vendor=self.vendor,purchase_order=self.po,component=other,quantity_received=2,received_date=date.today(),qc_passed_rows=[{'id':1,'serial_number':'MOTOR-1','qty':1},{'id':2,'serial_number':'MOTOR-2','qty':1}])
        record_inward_costs(other_receipt)
        self.assertEqual(sum(SerialPurchaseCost.objects.values_list('allocated_cost',flat=True)),Decimal('380.60'))
        self.assertGreater(SerialPurchaseCost.objects.get(serial_number='MOTOR-1').allocated_cost,Decimal('100'))
        self.assertLess(SerialPurchaseCost.objects.filter(component=self.component).first().allocated_cost,Decimal('23'))

    def test_inventory_deduction_return_scrap_same_serial_value(self):
        receipt=self.receipt();numbers=self.qc(receipt,[1,2,3,4],[5,6,7,8])
        stock=Inventory.objects.create(inventory_code='INV-COST',component=self.component,quantity=4,received_date=date.today(),serial_numbers=numbers,total_price='180.60',purchase_order=self.po.po_number)
        stock.refresh_from_db();self.assertEqual(stock.total_price,Decimal('90.32'))
        removed=numbers[:2];stock.serial_numbers=numbers[2:];stock.issued_serial_numbers=removed;stock.quantity=2
        stock.save(update_fields=['serial_numbers','issued_serial_numbers','quantity'])
        stock.refresh_from_db();self.assertEqual(stock.total_price,Decimal('45.16'))
        scrap=OutwardEntry.objects.create(code='SCRAP-COST',component=self.component,quantity=2,serial_numbers=removed,out_date=date.today())
        group=OutwardEntrySerializer(scrap).data['cost_details'][0]
        self.assertEqual(group['totals']['allocated_cost'],'45.16')
        self.assertEqual(group['serials'][0]['vendor_name'],self.vendor.name)
        returned=Inventory.objects.create(inventory_code='INV-RETURN',component=self.component,quantity=2,received_date=date.today(),serial_numbers=removed,total_price=0)
        returned.refresh_from_db();self.assertEqual(returned.total_price,Decimal('45.16'))

    def test_direct_invoice_and_missing_cost(self):
        receipt=self.receipt(po=False)
        InwardLineItem.objects.create(inward_entry=receipt,quantity=8,unit_price=20,gst_percentage=0,grand_total='180.60')
        self.qc(receipt,list(range(1,9)))
        self.assertEqual(sum(SerialPurchaseCost.objects.values_list('allocated_cost',flat=True)),Decimal('180.60'))
        missing=InwardEntry.objects.create(code='INW-UNKNOWN',vendor=self.vendor,component=self.component,quantity_received=1,received_date=date.today())
        self.assertIn('cost_error',inward_cost_details(missing)[0])

    def test_manual_stock_preserves_opening_value_on_issue(self):
        stock=Inventory.objects.create(inventory_code='MANUAL',component=self.component,quantity=8,received_date=date.today(),serial_numbers=[f'MAN-{i}' for i in range(8)],total_price='180.60')
        stock.serial_numbers=stock.serial_numbers[1:];stock.issued_serial_numbers=['MAN-0'];stock.quantity=7;stock.save()
        stock.refresh_from_db();self.assertEqual(stock.total_price,Decimal('158.02'))
        self.assertEqual(InventorySerializer(stock).data['cost_details'][0]['totals']['allocated_cost'],'158.02')

    def test_unknown_scrap_is_not_zero_valued(self):
        scrap=OutwardEntry.objects.create(code='SCRAP-UNKNOWN',component=self.component,quantity=1,serial_numbers=['UNKNOWN'],out_date=date.today())
        group=OutwardEntrySerializer(scrap).data['cost_details'][0]
        self.assertFalse(group['cost_complete']);self.assertFalse(group['serials'][0]['cost_available'])

    def test_duplicate_serial_from_another_receipt_rejected(self):
        first=self.receipt();self.qc(first,[1])
        second=self.receipt('SECOND',po=False)
        InwardLineItem.objects.create(inward_entry=second,quantity=8,unit_price=20,grand_total=160)
        second.qc_passed_rows=first.qc_passed_rows;second.save()
        with self.assertRaises(ValidationError):record_inward_costs(second)

    def test_real_qc_endpoint_costs_only_passed_stock(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory,force_authenticate
        from inward.views import InwardEntryViewSet
        receipt=self.receipt()
        user=get_user_model().objects.create_user(email='cost-check@example.test',password='test-only',role='admin')
        rows=[{'id':i,'serialNumber':f'API-SERIAL-{i}','qty':1,'remarks':'QC OK'} for i in range(1,9)]
        request=APIRequestFactory().post(f'/inward/{receipt.pk}/qc/',{'passedRows':rows[:4],'failedRows':rows[4:]},format='json')
        force_authenticate(request,user=user)
        response=InwardEntryViewSet.as_view({'post':'qc'})(request,pk=receipt.pk)
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(SerialPurchaseCost.objects.count(),8)
        stock=Inventory.objects.get(component=self.component)
        self.assertEqual(stock.quantity,4)
        self.assertEqual(stock.total_price,Decimal('90.32'))

    def test_actual_fifo_issue_updates_stock_value(self):
        from inventory.views import ProjectInventoryViewSet
        receipt=self.receipt();numbers=self.qc(receipt,list(range(1,9)))
        stock=Inventory.objects.create(inventory_code='FIFO-STOCK',component=self.component,quantity=8,received_date=date.today(),serial_numbers=numbers,total_price='180.60',purchase_order=self.po.po_number)
        count,issued=ProjectInventoryViewSet.deduct_store_fifo(self.component.pk,2,selected_serials=numbers[-2:])
        self.assertEqual(count,2)
        stock.refresh_from_db()
        self.assertEqual(stock.total_price,Decimal('135.46'))
        self.assertEqual(issued,numbers[-2:])

    def test_multi_component_scrap_metadata_is_costed(self):
        receipt=self.receipt();numbers=self.qc(receipt,[1,2])
        outward=OutwardEntry.objects.create(code='SCRAP-META',out_date=date.today(),quantity=2,
            inventory_allocations={'scrap_items':[{'component':self.component.pk,'quantity':2,'serial_numbers':numbers}]})
        detail=OutwardEntrySerializer(outward).data['cost_details'][0]
        self.assertEqual(detail['component_name'],'Wings');self.assertEqual(detail['totals']['allocated_cost'],'45.16')

    def test_material_request_and_project_keep_issued_costs(self):
        from materialrequest.models import MaterialRequest
        from materialrequest.serializers import MaterialRequestSerializer
        from inventory.models import ProjectInventory
        mr=MaterialRequest.objects.create(material_request_id='MR-COST',request_type='BOM',project='Test project',required_date=date.today(),date=date.today())
        receipt=self.receipt();numbers=self.qc(receipt,[1,2])
        ProjectInventory.objects.create(material_request=mr,project='Test project',component=self.component,
            purchased_serial_numbers=numbers,issued_purchased_serials=numbers[:1],requested_quantity=2,
            purchased_quantity=2,issued_purchased_quantity=1)
        detail=MaterialRequestSerializer(mr).data['cost_details'][0]
        self.assertEqual(detail['totals']['allocated_cost'],'45.16')
        self.assertEqual(detail['serials'][0]['status'],'Issued')

    def test_backfill_preview_does_not_commit(self):
        from django.core.management import call_command
        from io import StringIO
        receipt=self.receipt()
        receipt.qc_passed_rows=[{'id':1,'serial_number':'LEGACY-COST','qty':1}];receipt.save()
        call_command('backfill_serial_costs',stdout=StringIO(),stderr=StringIO())
        self.assertEqual(SerialPurchaseCost.objects.count(),0)
        call_command('backfill_serial_costs',apply=True,stdout=StringIO(),stderr=StringIO())
        self.assertEqual(SerialPurchaseCost.objects.count(),1)
