# scraper/admin.py (Modified section in ScrapeBatchAdmin)
import pandas as pd
from django import forms
from django.contrib import admin, messages  # Import messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse

from .models import ISSNQuery, ScrapeBatch
from .tasks import process_batch_task  # Import the batch task


class ExcelUploadForm(forms.Form):
    excel_file = forms.FileField(label="Upload ISSN Excel File")

@admin.register(ScrapeBatch)
class ScrapeBatchAdmin(admin.ModelAdmin):
    list_display = ('batch_id', 'original_filename', 'uploaded_at', 'status', 'get_issn_query_count', 'celery_group_id', 'view_issn_queries_link')
    list_filter = ('status', 'uploaded_at')
    readonly_fields = ('batch_id', 'uploaded_at', 'celery_group_id', 'notes') # notes also readonly
    actions = ['process_selected_batches_action']

    def get_issn_query_count(self, obj):
        return obj.issn_queries.count()
    get_issn_query_count.short_description = 'ISSN Queries'

    def view_issn_queries_link(self, obj):
        from django.utils.html import format_html
        count = obj.issn_queries.count()
        url = (
            reverse("admin:scraper_issnquery_changelist")
            + f"?batch__id__exact={obj.id}"
        )
        return format_html('<a href="{}">{} Queries</a>', url, count)
    view_issn_queries_link.short_description = 'View Queries'


    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path('upload-excel/', self.admin_site.admin_view(self.upload_excel_view), name='scraper_scrapebatch_upload_excel'),
        ]
        return custom_urls + urls

    def upload_excel_view(self, request):
        if request.method == 'POST':
            form = ExcelUploadForm(request.POST, request.FILES)
            if form.is_valid():
                excel_file = request.FILES['excel_file']
                batch = None # Initialize batch
                try:
                    batch = ScrapeBatch.objects.create(original_filename=excel_file.name, status=ScrapeBatch.Status.PENDING)
                    
                    df = pd.read_excel(excel_file)
                    if 'ISSN' not in df.columns:
                        messages.error(request, "Excel file must contain an 'ISSN' column.")
                        batch.status = ScrapeBatch.Status.FAILED
                        batch.notes = "Missing 'ISSN' column in uploaded file."
                        batch.save()
                        return HttpResponseRedirect(request.path_info)

                    issn_values = df['ISSN'].dropna().astype(str).str.strip().unique()
                    
                    created_queries = 0
                    skipped_issns = []
                    if not issn_values.any():
                        messages.warning(request, "No valid ISSNs found in the 'ISSN' column.")
                        batch.status = ScrapeBatch.Status.FAILED
                        batch.notes = "No ISSNs found in the 'ISSN' column."
                        batch.save()
                        return HttpResponseRedirect(request.path_info)

                    for issn_val in issn_values:
                        issn_cleaned = issn_val.replace('-', '')
                        if len(issn_cleaned) == 8 and issn_cleaned.isalnum(): # Basic ISSN validation
                            # Check for duplicates within the same batch (optional, but good)
                            if not ISSNQuery.objects.filter(batch=batch, issn=issn_cleaned).exists():
                                ISSNQuery.objects.create(batch=batch, issn=issn_cleaned)
                                created_queries +=1
                            else:
                                logger.info(f"Duplicate ISSN {issn_cleaned} in batch {batch.id} skipped.")
                        else:
                            skipped_issns.append(issn_val)
                            logger.warning(f"Skipping invalid ISSN format: {issn_val} for batch {batch.id}")
                    
                    if skipped_issns:
                        messages.warning(request, f"Skipped invalid ISSN formats: {', '.join(skipped_issns)}")

                    if created_queries > 0:
                        # Dispatch the batch processing task
                        process_batch_task.delay(batch.id)
                        batch.status = ScrapeBatch.Status.PROCESSING # Set to processing as tasks are dispatched
                        batch.notes = f"{created_queries} ISSNs queued from {excel_file.name}. Batch processing started."
                        messages.success(request, f"Successfully uploaded {excel_file.name}. Batch {batch.batch_id} created with {created_queries} ISSNs and sent for processing.")
                    else:
                        batch.status = ScrapeBatch.Status.FAILED
                        batch.notes = f"No valid, unique ISSNs found to process from {excel_file.name}."
                        messages.error(request, "No valid, unique ISSNs found to process.")
                    
                    batch.save()
                    return HttpResponseRedirect(reverse('admin:scraper_scrapebatch_changelist'))

                except Exception as e:
                    logger.error(f"Error processing Excel file: {e}", exc_info=True)
                    messages.error(request, f"Error processing Excel file: {e}")
                    if batch: # Check if batch was created
                        batch.status = ScrapeBatch.Status.FAILED
                        batch.notes = f"Error processing Excel file: {e}"
                        batch.save()
                    return HttpResponseRedirect(request.path_info) # Stay on form page on error
            else: # Form not valid
                messages.error(request, "Form is not valid. Please upload a valid Excel file.")

        else: # GET request
            form = ExcelUploadForm()
        
        context = dict(
           self.admin_site.each_context(request),
           form=form,
           title="Upload ISSN Excel",
           opts=self.model._meta, # Important for admin template rendering
        )
        # Use a custom template or ensure the default one works with minimal context
        return admin.ModelAdmin.render_change_form(self, request, context, form_url='') # Use Admin render_change_form

    def process_selected_batches_action(self, request, queryset):
        processed_count = 0
        for batch in queryset.filter(status__in=[ScrapeBatch.Status.PENDING, ScrapeBatch.Status.FAILED, ScrapeBatch.Status.PARTIAL_COMPLETE]):
            # Re-queue pending ISSNs for this batch
            pending_issns_in_batch = ISSNQuery.objects.filter(batch=batch, status__in=[
                ISSNQuery.Status.PENDING, 
                ISSNQuery.Status.FAILED, # Allow re-queueing failed ones
                ISSNQuery.Status.VPN_ERROR,
                ISSNQuery.Status.GIGALIB_ERROR,
                ISSNQuery.Status.SCOPUS_LOGIN_ERROR,
                ISSNQuery.Status.SCOPUS_SEARCH_ERROR,
                ISSNQuery.Status.SCOPUS_EXPORT_ERROR,
            ])
            if pending_issns_in_batch.exists():
                # Reset status of these ISSNs to PENDING before re-processing
                pending_issns_in_batch.update(status=ISSNQuery.Status.PENDING, celery_task_id=None, error_message=None, processing_started_at=None, processing_finished_at=None)
                
                process_batch_task.delay(batch.id)
                batch.status = ScrapeBatch.Status.PROCESSING
                batch.notes = f"Re-processing initiated for {pending_issns_in_batch.count()} ISSNs."
                batch.save()
                processed_count += 1
            else:
                messages.warning(request, f"Batch {batch.batch_id} has no re-processable ISSN queries.")
        
        if processed_count > 0:
            self.message_user(request, f"Successfully re-queued processing for {processed_count} batch(es).")
        else:
            self.message_user(request, "No batches were eligible for re-processing or had re-processable ISSNs.", level=messages.WARNING)

    process_selected_batches_action.short_description = "Re-process selected batches (Pending/Failed ISSNs)"


@admin.register(ISSNQuery)
class ISSNQueryAdmin(admin.ModelAdmin):
    list_display = ('issn', 'get_batch_id_link', 'status', 'celery_task_id', 'article_count', 'updated_at', 'result_csv_path', 'get_error_message_short')
    list_filter = ('status', 'batch__uploaded_at', 'batch__batch_id')
    search_fields = ('issn', 'celery_task_id', 'batch__batch_id')
    readonly_fields = ('batch', 'issn', 'created_at', 'updated_at', 'processing_started_at', 'processing_finished_at', 'celery_task_id', 'result_csv_path', 'article_count', 'error_message')
    list_per_page = 50
    
    def get_batch_id_link(self, obj):
        from django.utils.html import format_html
        url = reverse('admin:scraper_scrapebatch_change', args=[obj.batch.id])
        return format_html('<a href="{}">{}</a>', url, obj.batch.batch_id)
    get_batch_id_link.short_description = 'Batch ID'
    get_batch_id_link.admin_order_field = 'batch__batch_id'

    def get_error_message_short(self,obj):
        if obj.error_message:
            return (obj.error_message[:75] + '...') if len(obj.error_message) > 75 else obj.error_message
        return "-"
    get_error_message_short.short_description = 'Error (Short)'