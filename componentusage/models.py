from django.db import models

class ComponentUsage(models.Model):
    employee_name = models.CharField(max_length=100)
    component_name = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField()
    date = models.DateField()

    def __str__(self):
        return f"{self.employee_name} - {self.component_name}"
