document.addEventListener("DOMContentLoaded", () => {
    const dialog = document.getElementById("deviceProfileDialog");
    const form = document.getElementById("deviceProfileForm");
    if (!dialog || !form) return;
    const byId = (id) => document.getElementById(id);
    const fields = {
        name: "deviceName", device_type: "deviceType", platform: "devicePlatform", os_version: "deviceOsVersion",
        connection_type: "deviceConnection", manufacturer: "deviceManufacturer", model: "deviceModel", notes: "deviceNotes",
    };
    const showError = (message) => { const error = byId("deviceProfileFormError"); error.textContent = message; error.hidden = false; };
    const open = (profile = null) => {
        form.reset(); byId("deviceProfileFormError").hidden = true;
        byId("deviceProfileId").value = profile?.id || "";
        Object.entries(fields).forEach(([key, id]) => { if (profile?.[key]) byId(id).value = profile[key]; });
        byId("deviceProfileDialogTitle").textContent = profile ? "Edit device profile" : "Create device profile";
        byId("saveDeviceProfile").textContent = profile ? "Save changes" : "Save and use device";
        dialog.showModal();
    };
    const close = () => dialog.close();
    document.querySelectorAll("[data-open-device-form]").forEach((button) => button.addEventListener("click", () => open()));
    document.querySelectorAll(".edit-device").forEach((button) => button.addEventListener("click", () => open(JSON.parse(button.dataset.profile))));
    byId("closeDeviceProfileDialog").addEventListener("click", close);
    byId("cancelDeviceProfile").addEventListener("click", close);
    dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });
    form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (!form.reportValidity()) return;
        const id = byId("deviceProfileId").value;
        const payload = Object.fromEntries(Object.entries(fields).map(([key, fieldId]) => [key, byId(fieldId).value.trim()]));
        payload.activate = !id;
        try {
            const response = await fetch(id ? `/api/device-profiles/${id}` : "/api/device-profiles", {method: id ? "PATCH" : "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)});
            const result = await response.json(); if (!response.ok) throw new Error(result.error || "Device profile could not be saved.");
            window.location.reload();
        } catch (error) { showError(error.message); }
    });
    document.querySelectorAll(".activate-device").forEach((button) => button.addEventListener("click", async () => {
        const response = await fetch(`/api/device-profiles/${button.dataset.profileId}/activate`, {method:"POST"});
        if (response.ok) window.location.reload();
    }));
    document.querySelectorAll(".delete-device").forEach((button) => button.addEventListener("click", async () => {
        if (!window.confirm(`Delete ${button.dataset.profileName}? This cannot be undone.`)) return;
        const response = await fetch(`/api/device-profiles/${button.dataset.profileId}`, {method:"DELETE"});
        if (response.ok) window.location.reload();
    }));
});
