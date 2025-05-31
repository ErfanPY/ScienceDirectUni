# scraper/models.py
from django.db import models
from django.utils import timezone
import uuid

class ScrapeBatch(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PROCESSING = 'PROCESSING', 'Processing'
        PARTIAL_COMPLETE = 'PARTIAL_COMPLETE', 'Partially Completed'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'Failed'

    batch_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    uploaded_at = models.DateTimeField(default=timezone.now)
    original_filename = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    celery_group_id = models.CharField(max_length=255, blank=True, null=True, help_text="Celery Group ID if multiple tasks are launched for this batch")
    notes = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Batch {self.batch_id} ({self.status})"

class ISSNQuery(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        PROCESSING = 'PROCESSING', 'Processing'
        VPN_ERROR = 'VPN_ERROR', 'VPN Connection Error'
        GIGALIB_ERROR = 'GIGALIB_ERROR', 'Gigalib Navigation Error'
        SCOPUS_LOGIN_ERROR = 'SCOPUS_LOGIN_ERROR', 'Scopus Login/Access Error'
        SCOPUS_SEARCH_ERROR = 'SCOPUS_SEARCH_ERROR', 'Scopus Search Error'
        SCOPUS_EXPORT_ERROR = 'SCOPUS_EXPORT_ERROR', 'Scopus Export Error'
        NO_RESULTS = 'NO_RESULTS', 'No Results Found'
        COMPLETED = 'COMPLETED', 'Completed'
        FAILED = 'FAILED', 'General Failure' # Generic failure

    batch = models.ForeignKey(ScrapeBatch, related_name='issn_queries', on_delete=models.CASCADE)
    issn = models.CharField(max_length=9, help_text="e.g., 0142-6001") # ISSN format XXXXXXXX or XXXX-XXXX
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    celery_task_id = models.CharField(max_length=255, blank=True, null=True, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    result_csv_path = models.CharField(max_length=512, blank=True, null=True, help_text="Path to the downloaded CSV file")
    article_count = models.IntegerField(null=True, blank=True, help_text="Number of articles found/exported")
    error_message = models.TextField(blank=True, null=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"ISSN {self.issn} ({self.status}) - Batch {self.batch.batch_id}"

# Optional: If you parse CSVs and store articles individually
# class ScrapedArticle(models.Model):
#     issn_query = models.ForeignKey(ISSNQuery, related_name='articles', on_delete=models.CASCADE)
#     title = models.TextField()
#     authors = models.TextField(blank=True, null=True)
#     year = models.IntegerField(null=True, blank=True)
#     source_title = models.CharField(max_length=500, blank=True, null=True)
#     # ... other fields from your CSV ...
#     raw_data = models.JSONField(null=True, blank=True) # Store the raw row data

#     def __str__(self):
#         return self.title[:100]