from enum import Enum
from itertools import groupby
from operator import attrgetter
from sys import maxsize
from typing import List, Type, Dict
from urllib.parse import quote_plus

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import QuerySet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.defaults import (
    page_not_found as dj_page_not_found,
    permission_denied as dj_permission_denied,
    server_error as dj_server_error
)
from sitecats.toolbox import get_category_lists, get_category_aliases_under
from sitegate.decorators import signin_view, signup_view, redirect_signedin
from sitegate.signup_flows.classic import SimpleClassicWithEmailSignup
from sitemessage.toolbox import get_user_preferences_for_ui, set_user_preferences_from_request
from xross.toolbox import xross_view
from xross.utils import XrossHandlerBase

from .exceptions import RedirectRequired
from .generics.views import DetailsView, RealmView, EditView, ListingView, HttpRequest
from .integration.telegram import handle_request
from .models import Place, User, Community, Event, Reference, Vacancy, ExternalResource, ReferenceMissing, \
    Category, Person, PEP
from .utils import message_warning, search_models


class UserDetailsView(DetailsView):
    """Представление с детальной информацией о пользователе."""

    def check_view_permissions(self, request: HttpRequest, item: User):
        super().check_view_permissions(request, item)

        if not item.profile_public and item != request.user:
            # Закрываем доступ к непубличным профилям.
            raise PermissionDenied()

    def update_context(self, context: dict, request: HttpRequest):

        user = context['item']
        context['bookmarks'] = user.get_bookmarks()
        context['stats'] = lambda: user.get_stats()  # Ленивость для кеша в шаблоне

        if user == request.user:
            context['drafts'] = user.get_drafts()


class PersonDetailsView(DetailsView):
    """Представление с детальной информацией о персоне."""

    def update_context(self, context: dict, request: HttpRequest):
        user = context['item']
        context['materials'] = lambda: user.get_materials()  # Ленивость для кеша в шаблоне


class UserEditView(EditView):
    """Представление редактирования пользователя."""

    def check_edit_permissions(self, request: HttpRequest, item: User):
        # Пользователи не могут редактировать других пользователей.
        if item != request.user:
            raise PermissionDenied()

    def update_context(self, context: dict, request: HttpRequest):

        if request.POST:
            prefs_were_set = set_user_preferences_from_request(request)

            if prefs_were_set:
                raise RedirectRequired()

        subscr_prefs = get_user_preferences_for_ui(request.user, new_messengers_titles={
            'twitter': '<i class="fi-social-twitter" title="Twitter"></i>',
            'smtp': '<i class="fi-mail" title="Эл. почта"></i>'
        })

        context['subscr_prefs'] = subscr_prefs


class PlaceDetailsView(DetailsView):
    """Представление с детальной информацией о месте."""

    def set_im_here(self, request: HttpRequest, xross: XrossHandlerBase = None):
        """Используется xross. Прописывает место и часовой пояс в профиль пользователя.

        :param request:
        :param xross:

        """
        user = request.user

        if user.is_authenticated:
            user.place = xross.attrs['item']
            user.set_timezone_from_place()
            user.save()

    @xross_view(set_im_here)  # Метод перекрыт для добавления AJAX-обработчика.
    def get(self, request: HttpRequest, obj_id: int) -> HttpResponse:
        return super().get(request, obj_id)

    def update_context(self, context: dict, request: HttpRequest):
        place = context['item']

        if request.user.is_authenticated:
            context['allow_im_here'] = (request.user.place != place)

        context['users'] = User.get_actual().filter(place=place)
        context['communities'] = Community.get_actual().filter(place=place)
        context['events'] = Event.get_actual().filter(place=place)
        context['vacancies'] = Vacancy.get_actual().filter(place=place)
        context['stats_salary'] = Vacancy.get_salary_stats(place)


class PlaceListingView(RealmView):
    """Представление с картой и списком всех известных мест."""

    def get(self, request: HttpRequest) -> HttpResponse:
        places = Place.get_actual().order_by('-supporters_num', 'title')
        return self.render(request, {self.realm.name_plural: places})


class PepListingView(ListingView):
    """Представление со списком PEP."""

    def get_paginator_per_page(self, request: HttpRequest) -> int:
        if request.disable_paginator:
            return maxsize
        return super().get_paginator_per_page(request)

    def apply_object_filter(self, *, attrs: Dict[str, Type[Enum]], objects: QuerySet):

        applied = False

        for attr, enum in attrs.items():
            val = self.request.GET.get(attr)

            if val and val.isdigit():

                val = int(val)

                if val in enum.values:
                    objects = objects.filter(**{attr: val})
                    applied = True

        return applied, objects

    def get_paginator_objects(self) -> QuerySet:

        objects = super().get_paginator_objects()

        applied, objects = self.apply_object_filter(attrs={
            'status': PEP.Status,
            'type': PEP.Type,
        }, objects=objects)

        self.request.disable_paginator = applied

        return objects


class VacancyListingView(ListingView):
    """Представление со списком вакансий."""

    def update_context(self, context: dict, request: HttpRequest):
        context['stats_salary'] = Vacancy.get_salary_stats()
        context['stats_places'] = Vacancy.get_places_stats()

    def get_most_voted_objects(self) -> List:
        return []


class ReferenceListingView(RealmView):
    """Представление со списком справочников."""

    def get(self, request: HttpRequest) -> HttpResponse:
        # Справочник один, поэтому перенаправляем сразу на него.
        return redirect(self.realm.get_details_urlname(slugged=True), 'python', permanent=True)


class ReferenceDetailsView(DetailsView):
    """Представление статьи справочника."""

    def update_context(self, context: dict, request: HttpRequest):

        reference = context['item']
        context['children'] = reference.get_actual(parent=reference).order_by('title')

        if reference.parent is not None:
            context['siblings'] = reference.get_actual(parent=reference.parent, exclude_id=reference.id).order_by('title')


class CategoryListingView(RealmView):
    """Выводит список известных категорий, либо список сущностей для конкретной категории."""

    def get(self, request: HttpRequest, obj_id: int = None):
        from .realms import get_realms

        realms = get_realms().values()

        if obj_id is None:  # Запрошен список всех известных категорий.
            item = get_category_lists(
                init_kwargs={
                    'show_title': True,
                    'show_links': lambda cat: reverse(self.realm.get_details_urlname(), args=[cat.id])
                },
                additional_parents_aliases=get_category_aliases_under())

            return self.render(request, {'item': item, 'realms': realms})

        # Выводим список материалов (разбитых по областям сайта) для конкретной категории.
        category = get_object_or_404(Category.objects.select_related('parent'), pk=obj_id)

        realms_links = {}

        for realm in realms:
            realm_model = realm.model

            if not hasattr(realm_model, 'categories'):  # ModelWithCategory
                continue

            items = realm_model.get_objects_in_category(category)

            if not items:
                continue

            realm_title = realm_model.get_verbose_name_plural()

            _, plural = realm.get_names()

            realms_links[realm_title] = (plural, items)

        return self.render(request, {self.realm.name: category, 'item': category, 'realms_links': realms_links})


class VersionDetailsView(DetailsView):
    """Представление с детальной информацией о версии Питона."""

    def update_context(self, context: dict, request: HttpRequest):
        version = context['item']
        context['added'] = version.reference_added.order_by('title')
        context['deprecated'] = version.reference_deprecated.order_by('title')
        context['peps'] = version.peps.order_by('num')


# Наши страницы ошибок.
def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return dj_permission_denied(request, exception, template_name='static/403.html')


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return dj_page_not_found(request, exception, template_name='static/404.html')


def server_error(request: HttpRequest):
    return dj_server_error(request, template_name='static/500.html')


@cache_page(1800)  # 30 минут
@csrf_protect
def index(request: HttpRequest) -> HttpResponse:
    """Индексная страница."""
    from .realms import get_realms

    realms_data = []
    realms_registry = get_realms()

    externals = ExternalResource.objects.filter(realm_name__in=realms_registry.keys())
    externals = {k: list(v) for k, v in groupby(externals, attrgetter('realm_name'))}

    max_additional = 5

    for name, realm in realms_registry.items():

        if not realm.show_on_main:
            continue

        realm_externals = externals.get(name, [])
        count_externals = len(realm_externals)

        count_locals = 1
        if count_externals < max_additional:
            count_locals += max_additional - count_externals

        realm_locals = realm.model.get_actual()[:count_locals]

        if realm_locals:
            main = realm_locals[0]
            additional = list(realm_locals[1:]) + realm_externals[:max_additional]

        else:
            main = {}
            additional = []

        realms_data.append({
            'cls': realm,
            'main': main,
            'additional': additional,
        })

    return render(request, 'index.html', {'realms_data': realms_data})


@csrf_exempt
def telebot(request: HttpRequest) -> HttpResponse:
    """Обрабатывает запросы от Telegram.

    :param request:

    """
    handle_request(request)
    return HttpResponse()


def search(request: HttpRequest) -> HttpResponse:
    """Страница с результатами поиска по справочнику.
    Если найден один результат, перенаправляет на страницу результата.

    """

    search_term, results = search_models(
        request.POST.get('text', ''), search_in=(
            Category,
            Person,
            Reference,
        ))

    if not search_term:
        return redirect('index')

    if not results:
        # Поиск не дал результатов. Запомним, что искали и сообщим администраторам,
        # чтобы приняли меры по возможности.

        ReferenceMissing.add(search_term)

        message_warning(
            request, 'Поиск по справочнику и категориям не дал результатов, '
                     'и мы переключились на поиск по всему сайту.')

        # Перенаправляем на поиск по всему сайту.
        redirect_response = redirect('search_site')
        redirect_response['Location'] += f'?searchid={settings.YANDEX_SEARCH_ID}&text={quote_plus(search_term)}'

        return redirect_response

    results_len = len(results)

    if results_len == 1:
        return redirect(results[0].get_absolute_url())

    return render(request, 'static/search.html', {
        'search_term': search_term,
        'results': results,
        'results_len': results_len,
    })


@redirect_signedin
@signin_view(
    widget_attrs={'class': 'form-control', 'placeholder': lambda f: f.label},
    template='form_bootstrap4'
)
@signup_view(
    widget_attrs={'class': 'form-control', 'placeholder': lambda f: f.label},
    template='form_bootstrap4',
    flow=SimpleClassicWithEmailSignup,
    verify_email=True
)
def login(request: HttpRequest) -> HttpResponse:
    """Страница авторизации и регистрации."""
    return render(request, 'static/login.html')


@login_required
def user_settings(request: HttpRequest) -> HttpResponse:
    """Перенаправляет на страницу настроек текущего пользователя."""
    return redirect('users:edit', request.user.pk)


def ide(request: HttpRequest) -> HttpResponse:
    """Страница подсказок для IDE."""

    term = request.GET.get('term', '')
    results = []
    error = ''

    ide_version = request.headers.get('Ide-Version')
    ide_name = request.headers.get('Ide-Name')

    if ide_version and ide_name:

        if ide_name in {'IntelliJ IDEA', 'PyCharm'}:
            term, results = search_models(term, search_in=(Reference,))

        else:
            error = f'Используемая вами среда разработки "{ide_name} {ide_version}" не поддерживается.'

    return render(request, 'realms/references/ide.html', {'term': term, 'results': results, 'error': error})
