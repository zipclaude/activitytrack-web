/* SPDX-License-Identifier: GPL-3.0-or-later */
/*
 * The pages' small behaviours, from data attributes: no inline script, so
 * the Content-Security-Policy allows scripts from this site only.
 *   data-autosubmit    on a form or a field: a change submits the form
 *   data-confirm       on a form (submit) or a button (click): asks first
 *   data-policy-user   the policy index: opens the chosen user's policy
 *   data-when          the new-activity form: the fields of "now", "started", "past"
 *   data-profile       the user form: the parts of the chosen profile
 */
(function () {
	"use strict";
	document.addEventListener("change", function (ev) {
		var el = ev.target.closest("[data-autosubmit]");
		if (el && ev.target.form)
			ev.target.form.submit();
	});
	document.addEventListener("submit", function (ev) {
		var form = ev.target;
		if (form.dataset.confirm && !window.confirm(form.dataset.confirm))
			ev.preventDefault();
		if (form.dataset.policyUser) {
			ev.preventDefault();
			location.href = form.dataset.policyUser.replace("0", form.elements.u.value);
		}
	});
	document.addEventListener("click", function (ev) {
		var b = ev.target.closest("button[data-confirm]");
		if (b && !window.confirm(b.dataset.confirm))
			ev.preventDefault();
	});

	var when = document.querySelector("form[data-when]");
	if (when) {
		var syncWhen = function () {
			var v = when.querySelector("input[name=when]:checked").value;
			when.querySelectorAll(".when-started, .when-past").forEach(function (el) { el.hidden = !el.classList.contains("when-" + v); });
			when.querySelector("button.primary").textContent = v === "past" ? when.dataset.labelPast : when.dataset.labelNow;
		};
		when.addEventListener("change", syncWhen);
		syncWhen();
	}

	var profile = document.querySelector("select[data-profile]");
	if (profile) {
		var syncProfile = function () {
			document.querySelectorAll("[data-for]").forEach(function (el) {
				el.hidden = el.dataset.for.split(" ").indexOf(profile.value) < 0;
			});
		};
		profile.addEventListener("change", syncProfile);
		syncProfile();
	}
})();
