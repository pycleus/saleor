# Generated by Django 3.2.20 on 2023-07-11 16:10

from django.db import migrations, models
import django.db.models.deletion


def assign_products_to_attribute_values(apps, schema_editor):
    AssignedProductAttributeValue = apps.get_model(
        "attribute", "AssignedProductAttributeValue"
    )

    for attribute_value in AssignedProductAttributeValue.objects.all():
        attribute_value.new_product = attribute_value.assignment.product
        attribute_value.save()


class Migration(migrations.Migration):
    dependencies = [
        ("product", "0186_product_new_attributes"),
        ("attribute", "0029_alter_attribute_unit"),
    ]

    operations = [
        migrations.AddField(
            model_name="assignedproductattributevalue",
            name="new_product",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="attributevalues",
                to="product.product",
            ),
        ),
        migrations.RunPython(assign_products_to_attribute_values),
        migrations.AlterField(
            model_name="assignedproductattributevalue",
            name="new_product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="attributevalues",
                to="product.product",
            ),
        ),
        migrations.RemoveField(
            model_name="attributeproduct",
            name="assigned_products",
        ),
        migrations.AlterUniqueTogether(
            name="assignedproductattributevalue",
            unique_together={("value", "new_product")},
        ),
        migrations.RemoveField(
            model_name="assignedproductattributevalue",
            name="assignment",
        ),
        migrations.DeleteModel(
            name="AssignedProductAttribute",
        ),
    ]
