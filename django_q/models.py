import importlib
import logging

from django.core.urlresolvers import reverse
from django.utils.translation import ugettext_lazy as _
from django.db import models
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.utils import timezone
from picklefield import PickledObjectField


class Task(models.Model):
    id = models.CharField(max_length=32, primary_key=True, editable=False)
    name = models.CharField(max_length=100, editable=False)
    func = models.CharField(max_length=256)
    hook = models.CharField(max_length=256, null=True)
    args = PickledObjectField(null=True)
    kwargs = PickledObjectField(null=True)
    result = PickledObjectField(null=True)
    started = models.DateTimeField(editable=False)
    stopped = models.DateTimeField(editable=False)
    success = models.BooleanField(default=True, editable=False)

    @staticmethod
    def get_result(task_id):
        if len(task_id) == 32 and Task.objects.filter(id=task_id).exists():
            return Task.objects.get(id=task_id).result
        elif Task.objects.filter(name=task_id).exists():
            return Task.objects.get(name=task_id).result

    @staticmethod
    def get_task(task_id):
        if len(task_id) == 32 and Task.objects.filter(id=task_id).exists():
            return Task.objects.get(id=task_id)
        elif Task.objects.filter(name=task_id).exists():
            return Task.objects.get(name=task_id)

    def time_taken(self):
        return (self.stopped - self.started).total_seconds()

    def __unicode__(self):
        return self.name

    class Meta:
        app_label = 'django_q'


@receiver(pre_save, sender=Task)
def call_hook(sender, instance, **kwargs):
    if instance.hook:
        logger = logging.getLogger('django-q')
        f = instance.hook
        if not callable(f):
            try:
                module, func = f.rsplit('.', 1)
                m = importlib.import_module(module)
                f = getattr(m, func)
            except (ValueError, ImportError, AttributeError):
                logger.error(_('malformed return hook \'{}\' for [{}]').format(instance.hook, instance.name))
                return
        try:
            f(instance)
        except Exception as e:
            logger.error(_('return hook {} failed on [{}] because {}').format(instance.hook, instance.name, e))


class SuccessManager(models.Manager):
    def get_queryset(self):
        return super(SuccessManager, self).get_queryset().filter(
            success=True)


class Success(Task):
    objects = SuccessManager()

    class Meta:
        app_label = 'django_q'
        verbose_name = _('Successful task')
        verbose_name_plural = _('Successful tasks')
        proxy = True


class FailureManager(models.Manager):
    def get_queryset(self):
        return super(FailureManager, self).get_queryset().filter(
            success=False)


class Failure(Task):
    objects = FailureManager()

    class Meta:
        app_label = 'django_q'
        verbose_name = _('Failed task')
        verbose_name_plural = _('Failed tasks')
        proxy = True


class Schedule(models.Model):
    func = models.CharField(max_length=256, help_text='e.g. module.tasks.function')
    hook = models.CharField(max_length=256, null=True, blank=True, help_text='e.g. module.tasks.result_function')
    args = models.TextField(null=True, blank=True, help_text=_("e.g. 1, 2, 'John'"))
    kwargs = models.TextField(null=True, blank=True, help_text=_("e.g. x=1, y=2, name='John'"))
    ONCE = 'O'
    HOURLY = 'H'
    DAILY = 'D'
    WEEKLY = 'W'
    MONTHLY = 'M'
    QUARTERLY = 'Q'
    YEARLY = 'Y'
    TYPE = (
        (ONCE, _('Once')),
        (HOURLY, _('Hourly')),
        (DAILY, _('Daily')),
        (WEEKLY, _('Weekly')),
        (MONTHLY, _('Monthly')),
        (QUARTERLY, _('Quarterly')),
        (YEARLY, _('Yearly')),
    )
    schedule_type = models.CharField(max_length=1, choices=TYPE, default=TYPE[0][0], verbose_name=_('Schedule Type'))
    repeats = models.SmallIntegerField(default=-1, verbose_name=_('Repeats'), help_text=_('n = n times, -1 = forever'))
    next_run = models.DateTimeField(verbose_name=_('Next Run'), default=timezone.now, null=True)
    task = models.CharField(max_length=100, null=True, editable=False)

    def success(self):
        if self.task and Task.objects.filter(id=self.task):
            return Task.objects.get(id=self.task).success

    def last_run(self):
        if self.task and Task.objects.filter(id=self.task):
            task = Task.objects.get(id=self.task)
            if task.success:
                url = reverse('admin:django_q_success_change', args=(task.id,))
            else:
                url = reverse('admin:django_q_failure_change', args=(task.id,))
            return '<a href="{}">[{}]</a>'.format(url, self.task)
        return None

    def __unicode__(self):
        return self.func

    success.boolean = True
    last_run.allow_tags = True

    class Meta:
        app_label = 'django_q'
        verbose_name = _('Scheduled task')
        verbose_name_plural = _('Scheduled tasks')
        ordering = ['next_run']
