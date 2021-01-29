# ContentDB
# Copyright (C) 2018  rubenwardy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.


from celery import uuid
from flask import *
from flask_login import login_required
from flask_wtf import FlaskForm
from wtforms import *
from wtforms.ext.sqlalchemy.fields import QuerySelectField
from wtforms.validators import *

from app.rediscache import has_key, set_key, make_download_key
from app.tasks.importtasks import makeVCSRelease, checkZipRelease, check_update_config
from app.utils import *
from . import bp


def get_mt_releases(is_max):
	query = MinetestRelease.query.order_by(db.asc(MinetestRelease.id))
	if is_max:
		query = query.limit(query.count() - 1)
	else:
		query = query.filter(MinetestRelease.name != "0.4.17")

	return query


class CreatePackageReleaseForm(FlaskForm):
	title	   = StringField("Title", [InputRequired(), Length(1, 30)])
	uploadOpt  = RadioField ("Method", choices=[("upload", "File Upload")], default="upload")
	vcsLabel   = StringField("Git reference (ie: commit hash, branch, or tag)", default=None)
	fileUpload = FileField("File Upload")
	min_rel    = QuerySelectField("Minimum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(False), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	max_rel    = QuerySelectField("Maximum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(True), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	submit	   = SubmitField("Save")

class EditPackageReleaseForm(FlaskForm):
	title    = StringField("Title", [InputRequired(), Length(1, 30)])
	url      = StringField("URL", [Optional()])
	task_id  = StringField("Task ID", filters = [lambda x: x or None])
	approved = BooleanField("Is Approved")
	min_rel  = QuerySelectField("Minimum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(False), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	max_rel  = QuerySelectField("Maximum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(True), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	submit   = SubmitField("Save")

@bp.route("/packages/<author>/<name>/releases/new/", methods=["GET", "POST"])
@login_required
@is_package_page
def create_release(package):
	if not package.checkPerm(current_user, Permission.MAKE_RELEASE):
		return redirect(package.getDetailsURL())

	# Initial form class from post data and default data
	form = CreatePackageReleaseForm()
	if package.repo is not None:
		form["uploadOpt"].choices = [("vcs", "Import from Git"), ("upload", "Upload .zip file")]
		if request.method == "GET":
			form["uploadOpt"].data = "vcs"
			form.vcsLabel.data = request.args.get("ref")

	if request.method == "GET":
		form.title.data = request.args.get("title")

	if form.validate_on_submit():
		if form["uploadOpt"].data == "vcs":
			rel = PackageRelease()
			rel.package = package
			rel.title   = form["title"].data
			rel.url     = ""
			rel.task_id = uuid()
			rel.min_rel = form["min_rel"].data.getActual()
			rel.max_rel = form["max_rel"].data.getActual()
			db.session.add(rel)
			db.session.commit()

			makeVCSRelease.apply_async((rel.id, nonEmptyOrNone(form.vcsLabel.data)), task_id=rel.task_id)

			msg = "Release {} created".format(rel.title)
			addNotification(package.maintainers, current_user, NotificationType.PACKAGE_EDIT, msg, rel.getEditURL(), package)
			db.session.commit()

			return redirect(url_for("tasks.check", id=rel.task_id, r=rel.getEditURL()))
		else:
			uploadedUrl, uploadedPath = doFileUpload(form.fileUpload.data, "zip", "a zip file")
			if uploadedUrl is not None:
				rel = PackageRelease()
				rel.package = package
				rel.title = form["title"].data
				rel.url = uploadedUrl
				rel.task_id = uuid()
				rel.min_rel = form["min_rel"].data.getActual()
				rel.max_rel = form["max_rel"].data.getActual()
				db.session.add(rel)
				db.session.commit()

				checkZipRelease.apply_async((rel.id, uploadedPath), task_id=rel.task_id)

				msg = "Release {} created".format(rel.title)
				addNotification(package.maintainers, current_user, NotificationType.PACKAGE_EDIT, msg, rel.getEditURL(), package)
				db.session.commit()

				return redirect(url_for("tasks.check", id=rel.task_id, r=rel.getEditURL()))

	return render_template("packages/release_new.html", package=package, form=form)


@bp.route("/packages/<author>/<name>/releases/<id>/download/")
@is_package_page
def download_release(package, id):
	release = PackageRelease.query.get(id)
	if release is None or release.package != package:
		abort(404)

	ip = request.headers.get("X-Forwarded-For") or request.remote_addr
	if ip is not None and not is_user_bot():
		key = make_download_key(ip, release.package)
		if not has_key(key):
			set_key(key, "true")

			bonus = 1

			PackageRelease.query.filter_by(id=release.id).update({
					"downloads": PackageRelease.downloads + 1
				})

			Package.query.filter_by(id=package.id).update({
					"downloads": Package.downloads + 1,
					"score_downloads": Package.score_downloads + bonus,
					"score": Package.score + bonus
				})

			db.session.commit()

	return redirect(release.url, code=300)


@bp.route("/packages/<author>/<name>/releases/<id>/", methods=["GET", "POST"])
@login_required
@is_package_page
def edit_release(package, id):
	release = PackageRelease.query.get(id)
	if release is None or release.package != package:
		abort(404)

	canEdit	= package.checkPerm(current_user, Permission.MAKE_RELEASE)
	canApprove = package.checkPerm(current_user, Permission.APPROVE_RELEASE)
	if not (canEdit or canApprove):
		return redirect(package.getDetailsURL())

	# Initial form class from post data and default data
	form = EditPackageReleaseForm(formdata=request.form, obj=release)

	if request.method == "GET":
		# HACK: fix bug in wtforms
		form.approved.data = release.approved

	if form.validate_on_submit():
		wasApproved = release.approved
		if canEdit:
			release.title = form["title"].data
			release.min_rel = form["min_rel"].data.getActual()
			release.max_rel = form["max_rel"].data.getActual()

		if package.checkPerm(current_user, Permission.CHANGE_RELEASE_URL):
			release.url = form["url"].data
			release.task_id = form["task_id"].data
			if release.task_id is not None:
				release.task_id = None

		if canApprove:
			release.approved = form["approved"].data
		else:
			release.approved = wasApproved

		db.session.commit()
		return redirect(package.getDetailsURL())

	return render_template("packages/release_edit.html", package=package, release=release, form=form)



class BulkReleaseForm(FlaskForm):
	set_min = BooleanField("Set Min")
	min_rel  = QuerySelectField("Minimum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(False), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	set_max = BooleanField("Set Max")
	max_rel  = QuerySelectField("Maximum Minetest Version", [InputRequired()],
			query_factory=lambda: get_mt_releases(True), get_pk=lambda a: a.id, get_label=lambda a: a.name)
	only_change_none = BooleanField("Only change values previously set as none")
	submit   = SubmitField("Update")


@bp.route("/packages/<author>/<name>/releases/bulk_change/", methods=["GET", "POST"])
@login_required
@is_package_page
def bulk_change_release(package):
	if not package.checkPerm(current_user, Permission.MAKE_RELEASE):
		return redirect(package.getDetailsURL())

	# Initial form class from post data and default data
	form = BulkReleaseForm()

	if request.method == "GET":
		form.only_change_none.data = True
	elif form.validate_on_submit():
		only_change_none = form.only_change_none.data

		for release in package.releases.all():
			if form["set_min"].data and (not only_change_none or release.min_rel is None):
				release.min_rel = form["min_rel"].data.getActual()
			if form["set_max"].data and (not only_change_none or release.max_rel is None):
				release.max_rel = form["max_rel"].data.getActual()

		db.session.commit()

		return redirect(package.getDetailsURL())

	return render_template("packages/release_bulk_change.html", package=package, form=form)


@bp.route("/packages/<author>/<name>/releases/<id>/delete/", methods=["POST"])
@login_required
@is_package_page
def delete_release(package, id):
	release = PackageRelease.query.get(id)
	if release is None or release.package != package:
		abort(404)

	if not release.checkPerm(current_user, Permission.DELETE_RELEASE):
		return redirect(release.getEditURL())

	db.session.delete(release)
	db.session.commit()

	return redirect(package.getDetailsURL())


class PackageUpdateConfigFrom(FlaskForm):
	trigger = SelectField("Trigger", [InputRequired()], choices=PackageUpdateTrigger.choices(), coerce=PackageUpdateTrigger.coerce,
			default=PackageUpdateTrigger.TAG)
	ref     = StringField("Branch name", [Optional()], default=None)
	action  = SelectField("Action", [InputRequired()], choices=[("notification", "Notification"), ("make_release", "Create Release")], default="make_release")
	submit  = SubmitField("Save Settings")
	disable = SubmitField("Disable Automation")


@bp.route("/packages/<author>/<name>/update-config/", methods=["GET", "POST"])
@login_required
@is_package_page
def update_config(package):
	if not package.checkPerm(current_user, Permission.MAKE_RELEASE):
		abort(403)

	if not package.repo:
		flash("Please add a Git repository URL in order to set up automatic releases", "danger")
		return redirect(package.getEditURL())

	form = PackageUpdateConfigFrom(obj=package.update_config)
	if request.method == "GET":
		if package.update_config:
			form.action.data = "make_release" if package.update_config.make_release else "notification"
		elif request.args.get("action") == "notification":
			form.trigger.data = PackageUpdateTrigger.COMMIT
			form.action.data = "notification"

	if form.validate_on_submit():
		if form.disable.data:
			flash("Deleted update configuration", "success")
			if package.update_config:
				db.session.delete(package.update_config)
			db.session.commit()
		else:
			if package.update_config is None:
				package.update_config = PackageUpdateConfig()
				db.session.add(package.update_config)

			form.populate_obj(package.update_config)
			package.update_config.ref = nonEmptyOrNone(form.ref.data)
			package.update_config.make_release = form.action.data == "make_release"

			if package.update_config.last_commit is None:
				last_release = package.releases.first()
				if last_release and last_release.commit_hash:
					package.update_config.last_commit = last_release.commit_hash

			db.session.commit()

			if package.update_config.last_commit is None:
				check_update_config.delay(package.id)

		if not form.disable.data and package.releases.count() == 0:
			flash("Now, please create an initial release", "success")
			return redirect(package.getCreateReleaseURL())

		return redirect(package.getDetailsURL())

	return render_template("packages/update_config.html", package=package, form=form)


@bp.route("/packages/<author>/<name>/setup-releases/")
@login_required
@is_package_page
def setup_releases(package):
	if not package.checkPerm(current_user, Permission.MAKE_RELEASE):
		abort(403)

	if package.update_config:
		return redirect(package.getUpdateConfigURL())

	return render_template("packages/release_wizard.html", package=package)
