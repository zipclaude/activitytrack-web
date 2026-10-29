/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Polls the pairing state until the phone pairs or the code expires. */
(function () {
	const box = document.getElementById("pairing");
	if (!box) return;
	const show = (name) => box.querySelectorAll(".state").forEach((el) => { el.hidden = !el.hasAttribute("data-" + name); });
	async function poll() {
		try {
			const r = await fetch(box.dataset.status, { credentials: "same-origin" });
			const s = await r.json();
			if (s.state === "paired") {
				box.querySelector("[data-paired] b").textContent = s.device;
				box.querySelector(".qr").hidden = true;
				show("paired");
				return;
			}
			if (s.state === "expired") {
				box.querySelector(".qr").hidden = true;
				show("expired");
				return;
			}
		} catch (e) { /* retried */ }
		setTimeout(poll, 2000);
	}
	poll();
})();
