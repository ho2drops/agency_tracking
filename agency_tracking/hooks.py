app_name = "agency_tracking"
app_title = "Agency Tracking"
app_publisher = "Agency"
app_description = "Overseas Recruitment Processing Platform"
app_email = "admin@example.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "agency_tracking",
# 		"logo": "/assets/agency_tracking/logo.png",
# 		"title": "Agency Tracking",
# 		"route": "/agency_tracking",
# 		"has_permission": "agency_tracking.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/agency_tracking/css/agency_tracking.css"
# app_include_js = "/assets/agency_tracking/js/agency_tracking.js"

# include js, css files in header of web template
# web_include_css = "/assets/agency_tracking/css/agency_tracking.css"
# web_include_js = "/assets/agency_tracking/js/agency_tracking.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "agency_tracking/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "agency_tracking/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

website_route_rules = [
	{"from_route": "/swagger", "to_route": "docs"},
	{"from_route": "/api-docs", "to_route": "docs"},
]

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "agency_tracking.utils.jinja_methods",
# 	"filters": "agency_tracking.utils.jinja_filters"
# }

# Installation
# ------------

after_install = "agency_tracking.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "agency_tracking.uninstall.before_uninstall"
# after_uninstall = "agency_tracking.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "agency_tracking.utils.before_app_install"
# after_app_install = "agency_tracking.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "agency_tracking.utils.before_app_uninstall"
# after_app_uninstall = "agency_tracking.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "agency_tracking.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

permission_query_conditions = {
	"Process Event": "agency_tracking.agency_tracking.doctype.process_event.process_event.get_permission_query_conditions",
	"Clearance Step": "agency_tracking.agency_tracking.doctype.clearance_step.clearance_step.get_permission_query_conditions",
	"Applicant Transaction": "agency_tracking.agency_tracking.doctype.applicant_transaction.applicant_transaction.get_permission_query_conditions",
	"Commission Batch Request": "agency_tracking.agency_tracking.doctype.commission_batch_request.commission_batch_request.get_permission_query_conditions",
	# S-3: clearance-country roles see only their own cases (management + other internal roles unchanged).
	"Placement": "agency_tracking.agency_tracking.doctype.placement.placement.get_permission_query_conditions",
	"Applicant": "agency_tracking.agency_tracking.doctype.applicant.applicant.get_permission_query_conditions",
	"Background Job": "agency_tracking.agency_tracking.doctype.background_job.background_job.get_permission_query_conditions",
	# Unlike Commission Batch Request below, this one's JSON-granted roles (System
	# Manager/Admin) are NOT fully trusted by the query condition -- it scopes even them to
	# their own rows (only the literal Administrator user bypasses), so has_permission is
	# still load-bearing, not redundant.
	"Notification Read State": "agency_tracking.agency_tracking.doctype.notification_read_state.notification_read_state.get_permission_query_conditions",
}

# Single-document read/write gate. Background Job: load-bearing for its result PDF's own
# permission check (a private File's download falls through to has_permission("read") on its
# attached_to_doctype/_name), not just an audit-log-style role grant. Everything else here
# (2026-09-11): get_permission_query_conditions above only filters list/report queries, not a
# single frappe.get_doc()/doc.save()/REST /api/resource/<name> call -- without a matching
# has_permission hook, any role with blanket DocType-level read/write (every one of these
# doctypes' JSON grants it to roles the query-condition means to scope down) reaches every row
# by name, bypassing the scoping entirely, even rows that role can never see in any list.
# Audited all 7 permission_query_conditions entries above for this same gap; Commission Batch
# Request is the one exception -- its DocType JSON grants no role beyond
# Admin/Finance Manager/System Manager, all of which the query condition already fully trusts,
# so there's nothing for a single-doc call to bypass.
has_permission = {
	"Background Job": "agency_tracking.agency_tracking.doctype.background_job.background_job.has_permission",
	"Process Event": "agency_tracking.agency_tracking.doctype.process_event.process_event.has_permission",
	"Applicant Transaction": "agency_tracking.agency_tracking.doctype.applicant_transaction.applicant_transaction.has_permission",
	"Placement": "agency_tracking.agency_tracking.doctype.placement.placement.has_permission",
	"Applicant": "agency_tracking.agency_tracking.doctype.applicant.applicant.has_permission",
	"Clearance Step": "agency_tracking.agency_tracking.doctype.clearance_step.clearance_step.has_permission",
	"Notification Read State": "agency_tracking.agency_tracking.doctype.notification_read_state.notification_read_state.has_permission",
}

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# A file put into a photo / document / video slot must be of a kind that slot holds
# (upload_guard.SLOTS). Child-table slots are checked through these parents.
doc_events = {
	doctype: {"validate": "agency_tracking.upload_guard.check_slots"}
	for doctype in (
		"Applicant",
		"Placement",
		"Clearance Step",
		"Applicant Transaction",
		"Chat Message",
		"Bank Statement",
		"Agency Tracking Settings",
	)
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": [
		"agency_tracking.watchdogs.medical_expiry_watchdog",
		"agency_tracking.watchdogs.contract_age_watchdog",
		"agency_tracking.watchdogs.taeshir_injaz_reminder_watchdog",
		"agency_tracking.watchdogs.departure_due_watchdog",
		"agency_tracking.agency_tracking.doctype.notification_read_state.notification_read_state.cleanup_stale_read_state",
	],
	"hourly": [
		# maybe_fetch_fx_rates checks FX Rate Settings' mode/interval and no-ops unless
		# actually due (Part 7: Global/Custom FX mode) -- wired in finance_engine.py.
		"agency_tracking.finance_engine.maybe_fetch_fx_rates",
	],
	"cron": {
		# 2026-08-29: moved from Mon/Thu to Fri/Sat/Sun -- remind before the Monday
		# document-submission deadline, not after it's already passed.
		"0 9 * * 5,6,0": [
			"agency_tracking.watchdogs.wakala_reminder_watchdog",
		],
	},
}

# Testing
# -------

before_tests = "agency_tracking.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "agency_tracking.event.get_events"
# }

# Every upload goes through this app's own checks first (type, real content, size, agency files
# always private), then on to the framework's upload (QA P8-16).
override_whitelisted_methods = {"upload_file": "agency_tracking.upload_guard.upload_file"}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "agency_tracking.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Login Events
# ------------
# Part E: "retried on next login" — the other half of the offline-delivery guarantee (the
# other half is new Push Subscription registration, handled directly in
# notification_api.subscribe_to_push).
on_login = ["agency_tracking.notification_engine.retry_pending_notifications_on_login"]

# Request Events
# ----------------
# before_request = ["agency_tracking.utils.before_request"]
# after_request = ["agency_tracking.utils.after_request"]

# Job Events
# ----------
# before_job = ["agency_tracking.utils.before_job"]
# after_job = ["agency_tracking.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"agency_tracking.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

