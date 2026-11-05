/* SPDX-License-Identifier: GPL-3.0-or-later */
/* The offline maps pages: the estimate of an area as the form is filled in, and the progress of the builds. */
(function () {
	const MB = 1048576;

	// the form: tiles and size of the area, from the server (one formula, in one place)
	const form = document.getElementById("areaform");
	if (form) {
		const out = document.getElementById("estimate");
		let timer = 0;
		const ask = async () => {
			const q = new URLSearchParams();
			for (const k of ["lat", "lon", "radius_km", "max_zoom"]) q.set(k, form.elements[k].value.replace(",", "."));
			try {
				const r = await fetch(form.dataset.estimate + "?" + q, { credentials: "same-origin" });
				if (!r.ok) { out.textContent = ""; out.className = ""; return; }
				const s = await r.json();
				out.textContent = "≈ " + s.tiles + " tile, ≈ " + (s.bytes / MB).toFixed(1) + " MB";
				out.className = "note" + (s.too_big ? " error" : "");
			} catch (e) { /* the estimate is a help, not a need */ }
		};
		form.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(ask, 250); });
		ask();
	}

	// the list: the switch saves at once, the builds in progress update their row
	document.querySelectorAll("[data-autosubmit]").forEach((el) => el.addEventListener("change", () => el.form.submit()));
	const box = document.getElementById("areas");
	if (box && box.hasAttribute("data-busy")) {
		const poll = async () => {
			try {
				const r = await fetch(box.dataset.status, { credentials: "same-origin" });
				const s = await r.json();
				for (const a of s.areas) {
					const row = box.querySelector('[data-area="' + a.id + '"]');
					const p = row && row.querySelector("[data-progress]");
					if (p) p.textContent = a.progress;
				}
				// a finished build changes the whole row (state, size, buttons): load the page again
				if (!s.busy) { location.reload(); return; }
			} catch (e) { /* retried */ }
			setTimeout(poll, 3000);
		};
		setTimeout(poll, 3000);
	}
})();
