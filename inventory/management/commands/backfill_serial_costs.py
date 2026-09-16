from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.exceptions import ValidationError
from inward.models import InwardEntry
from inventory.models import Inventory
from inventory.costing import record_inward_costs, record_inventory_costs, serials, SerialPurchaseCost


class Command(BaseCommand):
    help = 'Preview serial cost backfill, or persist using --apply. Reports unresolved historical costs.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        # One transaction makes preview and apply run the identical resolver.
        with transaction.atomic():
            for inward in InwardEntry.objects.order_by('id').iterator():
                if not inward.qc_passed_rows and not inward.qc_failed_rows:
                    continue
                try:
                    with transaction.atomic():
                        record_inward_costs(inward)
                    self.stdout.write(f'Inward {inward.code}: cost records resolved')
                except (ValidationError, ValueError) as error:
                    self.stderr.write(f'Inward {inward.code}: unresolved: {error}')
            for stock in Inventory.objects.order_by('id').iterator():
                try:
                    with transaction.atomic():
                        # Already-issued manual stock has an ambiguous historical total.
                        # It is reported rather than priced from the remaining quantity.
                        record_inventory_costs(stock)
                    numbers = serials(stock.serial_numbers) + serials(stock.issued_serial_numbers)
                    known = SerialPurchaseCost.objects.filter(component_id=stock.component_id,
                        serial_number__in=numbers).count()
                    if known != len(set(numbers)):
                        self.stderr.write(f'Inventory {stock.inventory_code}: {len(set(numbers))-known} serial(s) have no verified purchase cost')
                except (ValidationError, ValueError) as error:
                    self.stderr.write(f'Inventory {stock.inventory_code}: unresolved: {error}')
            if not options['apply']:
                transaction.set_rollback(True)
                self.stdout.write('Preview only; no data saved. Use --apply to save the resolved costs.')
            else:
                self.stdout.write('Resolved serial costs saved. Unresolved records were left unchanged.')
