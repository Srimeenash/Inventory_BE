class CostDetailsSerializerMixin:
    """
    Add cost details to existing authorized resource representations.

    FAST LIST MODE
    --------------
    Add ?summary=1 to skip serial-level cost calculations and any fields
    declared in `summary_exclude_fields` on the serializer.

    Existing endpoints remain backward compatible because summary mode is
    opt-in. Detail pages can keep using the existing full response.

    Loading optimization
    --------------------
    Full responses can serialize the same component/project/cost source more
    than once through nested serializers. Cost results are therefore cached
    only inside the current serializer context/request.

    This does NOT change any costing formula or stored value.
    """

    SUMMARY_VALUES = {
        "1",
        "true",
        "yes",
        "on",
    }

    def is_summary_request(self):
        # Avoid parsing the same query parameter once for every row.
        if "_cost_summary_mode" in self.context:
            return self.context["_cost_summary_mode"]

        request = self.context.get("request")

        if request is None:
            summary_mode = False
        else:
            value = str(
                request.query_params.get(
                    "summary",
                    "",
                )
                or ""
            ).strip().lower()

            summary_mode = value in self.SUMMARY_VALUES

        self.context["_cost_summary_mode"] = summary_mode
        return summary_mode

    def get_fields(self):
        fields = super().get_fields()

        if not self.is_summary_request():
            return fields

        for field_name in getattr(
            self,
            "summary_exclude_fields",
            (),
        ):
            fields.pop(
                field_name,
                None,
            )

        return fields

    @staticmethod
    def _normalize_serial_cache_key(values):
        """
        Build a stable immutable key for serial lists without changing
        the order passed into the costing functions.
        """
        if not isinstance(values, (list, tuple)):
            return ()

        return tuple(
            str(value or "").strip()
            for value in values
        )

    def _cost_cache(self):
        return self.context.setdefault(
            "_cost_details_request_cache",
            {},
        )

    def _cached_cost_call(
        self,
        cache_key,
        callback,
    ):
        cache = self._cost_cache()

        if cache_key in cache:
            return cache[cache_key]

        value = callback()
        cache[cache_key] = value
        return value

    def to_representation(self, instance):
        data = super().to_representation(
            instance
        )

        # Fast table/list requests must not perform serial cost lookups.
        if self.is_summary_request() or self.context.get(
            "skip_cost_details",
            False,
        ):
            return data

        from .costing import (
            component_cost_details,
            inward_cost_details,
            outward_cost_details,
            project_cost_details,
        )

        label = instance._meta.label_lower

        if label == "inward.inwardentry":
            cache_key = (
                "inward",
                int(instance.pk or 0),
            )

            details = self._cached_cost_call(
                cache_key,
                lambda: inward_cost_details(
                    instance
                ),
            )

        elif label == "inventory.inventory":
            serial_numbers = (
                instance.serial_numbers
                if isinstance(
                    instance.serial_numbers,
                    list,
                )
                else []
            )

            cache_key = (
                "component",
                int(
                    instance.component_id
                    or 0
                ),
                self._normalize_serial_cache_key(
                    serial_numbers
                ),
                int(
                    instance.quantity
                    or 0
                ),
            )

            component_details = (
                self._cached_cost_call(
                    cache_key,
                    lambda: component_cost_details(
                        instance.component_id,
                        serial_numbers,
                        instance.quantity,
                    ),
                )
            )

            details = [
                component_details
            ]

        elif label == "inventory.projectinventory":
            cache_key = (
                "project",
                int(instance.pk or 0),
            )

            details = self._cached_cost_call(
                cache_key,
                lambda: project_cost_details(
                    instance
                ),
            )

        elif label == "outward.outwardentry":
            cache_key = (
                "outward",
                int(instance.pk or 0),
            )

            details = self._cached_cost_call(
                cache_key,
                lambda: outward_cost_details(
                    instance
                ),
            )

        elif label == "componentusage.componentusage":
            issued_serial_numbers = (
                instance.issued_serial_numbers
                if isinstance(
                    instance.issued_serial_numbers,
                    list,
                )
                else []
            )

            cache_key = (
                "component",
                int(
                    instance.component_id
                    or 0
                ),
                self._normalize_serial_cache_key(
                    issued_serial_numbers
                ),
                None,
            )

            component_details = (
                self._cached_cost_call(
                    cache_key,
                    lambda: component_cost_details(
                        instance.component_id,
                        issued_serial_numbers,
                    ),
                )
            )

            details = [
                component_details
            ]

        elif label == "materialrequest.materialrequest":
            details = []

            # This relation is already prefetched by the optimized
            # MaterialRequest ViewSet for non-summary responses.
            project_rows = (
                instance.project_inventory_items.all()
            )

            for project in project_rows:
                cache_key = (
                    "project",
                    int(project.pk or 0),
                )

                project_details = (
                    self._cached_cost_call(
                        cache_key,
                        lambda project=project:
                            project_cost_details(
                                project
                            ),
                    )
                )

                details.extend(
                    project_details
                )

        else:
            return data

        data["cost_details"] = details

        if (
            label == "inventory.inventory"
            and details
            and details[0].get(
                "cost_complete"
            )
        ):
            data["total_price"] = (
                details[0]
                .get(
                    "totals",
                    {},
                )
                .get(
                    "allocated_cost"
                )
            )

        return data
