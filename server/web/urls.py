# SPDX-License-Identifier: GPL-3.0-or-later
from django.contrib.auth import views as auth
from django.urls import path

from . import agenda, auth as login, mfa, masters, reports, users, views, work

urlpatterns = [
    path("", work.home, name="home"),
    path("attivita/", work.entries, name="entries"),
    path("attivita/nuova/", work.entry_new, name="entry_new"),
    path("attivita/<uuid:uid>/", work.entry, name="entry"),
    path("attivita/<uuid:uid>/comando/", work.quick, name="entry_quick"),
    path("allegati/<uuid:uid>/", work.attachment, name="attachment"),
    path("agenda/", agenda.agenda, name="agenda"),
    path("dafare/", work.todos, name="todos"),
    path("dafare/nuova/", work.todo_edit, name="todo_new"),
    path("dafare/<uuid:uid>/", work.todo_edit, name="todo_edit"),
    path("anagrafiche/", masters.index, name="masters_home"),
    path("anagrafiche/<str:kind>/", masters.index, name="masters"),
    path("anagrafiche/<str:kind>/nuova/", masters.edit, name="master_new"),
    path("anagrafiche/<str:kind>/<uuid:uid>/", masters.edit, name="master_edit"),
    path("report/", reports.report, name="report"),
    path("report/csv/", reports.report_csv, name="report_csv"),
    path("utenti/", users.users, name="users"),
    path("utenti/nuovo/", users.user_edit, name="user_new"),
    path("utenti/<int:pk>/", users.user_edit, name="user_edit"),
    path("utenti/<int:pk>/password/", users.user_password, name="user_password"),
    path("utenti/<int:pk>/verifica/", users.user_mfa_reset, name="user_mfa_reset"),
    path("password/", users.my_password, name="my_password"),
    path("login/", login.Login.as_view(), name="login"),
    path("login/codice/", mfa.login_step, name="login_code"),
    path("sicurezza/", mfa.security, name="security"),
    path("logout/", auth.LogoutView.as_view(), name="logout"),
    path("telefoni/", views.phones, name="phones"),
    path("telefoni/associa/", views.pair_new, name="pair_new"),
    path("telefoni/associa/<int:pk>/stato/", views.pair_status, name="pair_status"),
    path("telefoni/<uuid:pk>/revoca/", views.device_revoke, name="device_revoke"),
    path("telefoni/<uuid:pk>/gestionali/", views.device_holder, name="device_holder"),
]
