  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  function csrfFetch(input, init = {}) {
    if (String(init.method || "GET").toUpperCase() !== "POST") return fetch(input, init);
    const headers = new Headers(init.headers || {});
    headers.set("X-CSRF-Token", csrfToken);
    return fetch(input, {...init, headers});
  }
  const faceOverlay = document.getElementById("faceOverlay");
  const infoOverlay = document.getElementById("infoOverlay");
  const openFacesButton = document.querySelector("[data-open-faces]");
  const closeFacesButton = document.querySelector("[data-close-faces]");
  const openInfoButtons = document.querySelectorAll("[data-open-info]");
  const closeInfoButton = document.querySelector("[data-close-info]");
  const faceList = faceOverlay?.querySelector("[data-face-list]");
  const infoList = infoOverlay?.querySelector("[data-info-list]");
  const manualDateOverlay = document.getElementById("manualDateOverlay");
  const manualDateForm = document.querySelector("[data-manual-date-form]");
  const manualDateStatus = document.querySelector("[data-manual-date-status]");
  const closeManualDateButtons = document.querySelectorAll("[data-close-manual-date]");
  const clearManualDateButton = document.querySelector("[data-clear-manual-date]");
  const manualDateFields = document.querySelectorAll("[data-manual-date-field]");
  const personRenameDialog = document.getElementById("personRenameDialog");
  const personRenameForm = document.querySelector("[data-person-rename-form]");
  const personRenameStatus = document.querySelector("[data-person-rename-status]");
  const closePersonRenameButton = document.querySelector("[data-close-person-rename]");
  const faceSuggestDialog = document.getElementById("faceSuggestDialog");
  const openFaceSuggestButton = document.querySelector("[data-open-face-suggest]");
  const closeFaceSuggestButton = document.querySelector("[data-close-face-suggest]");
  const faceSuggestSuccess = document.querySelector("[data-face-suggest-success]");
  const faceSuggestStatus = document.querySelector("[data-face-suggest-status]");
  const personRenameNameInput = personRenameForm?.querySelector('input[name="new_name"]');
  const personRenameOldNameInput = personRenameForm?.querySelector('input[name="old_name"]');
  const searchForm = document.querySelector("[data-search-form]");
  const searchLoading = document.querySelector("[data-search-loading]");
  let facesLoaded = false;
  let infoLoaded = false;
  let infoFileId = "";
  let manualDateFileId = "";
  function isSettingsForm(form) {
    if (!form) return false;
    const action = new URL(form.getAttribute("action") || form.action || window.location.href, window.location.href);
    return action.pathname.startsWith("/settings/");
  }
  function setSettingsScrollField(form) {
    if (!isSettingsForm(form)) return;
    let input = form.querySelector('input[name="scroll_y"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "scroll_y";
      form.appendChild(input);
    }
    input.value = String(Math.max(0, Math.round(window.scrollY || 0)));
  }
  const settingsScrollRestore = document.querySelector("[data-settings-scroll-restore]");
  if (settingsScrollRestore) {
    const scrollY = Number(settingsScrollRestore.dataset.settingsScrollRestore || 0);
    if (Number.isFinite(scrollY) && scrollY > 0) {
      requestAnimationFrame(() => window.scrollTo({top: scrollY, left: 0}));
    }
  }
  function setMaintenanceStatusMessage(status, payload) {
    if (!status) return;
    if (Number(payload.missing) === 0) {
      status.textContent = "Oppdatert";
      return;
    }
    status.replaceChildren(
      document.createTextNode(`${payload.missing} bilder trenger ${payload.name}, kjør `),
      Object.assign(document.createElement("code"), {textContent: `bildebank ${payload.name}`}),
      document.createTextNode(" fra PowerShell.")
    );
  }
  function updateMaintenanceRow(row, payload) {
    const status = row?.querySelector("[data-maintenance-status]");
    const current = row?.querySelector("[data-maintenance-current]");
    const missing = row?.querySelector("[data-maintenance-missing]");
    const total = row?.querySelector("[data-maintenance-total]");
    if (current) current.textContent = String(payload.current);
    if (missing) missing.textContent = String(payload.missing);
    if (total) total.textContent = String(payload.total);
    setMaintenanceStatusMessage(status, payload);
  }
  function loadMaintenanceStatuses() {
    const maintenanceRows = Array.from(document.querySelectorAll("[data-maintenance-name]"));
    if (maintenanceRows.length === 0) return;
    maintenanceRows.forEach(row => {
      const status = row.querySelector("[data-maintenance-status]");
      if (status) status.textContent = "Oppdaterer...";
    });
    csrfFetch("/api/maintenance/statuses")
      .then(async response => {
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke hente vedlikeholdsstatus.");
        const statuses = new Map((payload.statuses || []).map(status => [status.name, status]));
        maintenanceRows.forEach(row => {
          const name = row.dataset.maintenanceName || "";
          const status = statuses.get(name);
          if (status) updateMaintenanceRow(row, status);
        });
      })
      .catch(error => {
        maintenanceRows.forEach(row => {
          const status = row.querySelector("[data-maintenance-status]");
          if (status) status.textContent = error.message || "Kunne ikke hente vedlikeholdsstatus.";
        });
      });
  }
  function scheduleMaintenanceStatusesLoad() {
    requestAnimationFrame(() => setTimeout(loadMaintenanceStatuses, 0));
  }
  if (document.readyState === "complete") {
    scheduleMaintenanceStatusesLoad();
  } else {
    window.addEventListener("load", scheduleMaintenanceStatusesLoad, {once: true});
  }
  const countThumbnailsButton = document.querySelector("[data-count-thumbnails]");
  countThumbnailsButton?.addEventListener("click", async () => {
    const row = countThumbnailsButton.closest("[data-thumbnail-maintenance]");
    const status = row?.querySelector("[data-thumbnail-status]");
    const current = row?.querySelector("[data-thumbnail-current]");
    const missing = row?.querySelector("[data-thumbnail-missing]");
    const total = row?.querySelector("[data-thumbnail-total]");
    const originalText = countThumbnailsButton.textContent;
    countThumbnailsButton.disabled = true;
    countThumbnailsButton.textContent = "Teller thumbnails...";
    if (status) status.textContent = "Teller thumbnails...";
    try {
      const response = await csrfFetch("/api/maintenance/thumbnails");
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke telle thumbnails.");
      if (current) current.textContent = String(payload.current);
      if (missing) missing.textContent = String(payload.missing);
      if (total) total.textContent = String(payload.total);
      if (status) {
        if (Number(payload.missing) === 0) {
          status.textContent = "Oppdatert";
        } else {
          status.replaceChildren(
            document.createTextNode(`${payload.missing} bilder mangler thumbnails, kjør `),
            Object.assign(document.createElement("code"), {textContent: "bildebank make-thumbnails"}),
            document.createTextNode(" fra PowerShell.")
          );
        }
      }
    } catch (error) {
      if (status) status.textContent = error.message || "Kunne ikke telle thumbnails.";
    } finally {
      countThumbnailsButton.disabled = false;
      countThumbnailsButton.textContent = originalText || "Tell thumbnails";
    }
  });
  document.addEventListener("change", event => {
    const form = event.target?.form;
    if (form) setSettingsScrollField(form);
  }, true);
  document.addEventListener("submit", event => {
    const message = event.submitter?.dataset.confirmSubmit;
    if (message && !confirm(message)) {
      event.preventDefault();
      return;
    }
    setSettingsScrollField(event.target);
  });
  openFaceSuggestButton?.addEventListener("click", () => {
    faceSuggestDialog.hidden = false;
    faceSuggestDialog.querySelector('input[name="threshold"]')?.focus();
  });
  closeFaceSuggestButton?.addEventListener("click", () => {
    faceSuggestDialog.hidden = true;
    if (faceSuggestSuccess) faceSuggestSuccess.hidden = true;
    if (faceSuggestStatus) {
      faceSuggestStatus.hidden = true;
      faceSuggestStatus.textContent = "";
    }
    closeFaceSuggestButton.textContent = "Avbryt";
  });
  const faceSuggestResult = new URLSearchParams(window.location.hash.slice(1)).get("face-suggest-status");
  if (faceSuggestDialog && faceSuggestStatus && faceSuggestResult) {
    if (faceSuggestSuccess) faceSuggestSuccess.hidden = false;
    faceSuggestStatus.textContent = faceSuggestResult;
    faceSuggestStatus.hidden = false;
    if (closeFaceSuggestButton) closeFaceSuggestButton.textContent = "Lukk";
    faceSuggestDialog.hidden = false;
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  function faceStatusMessage(message) {
    const item = document.createElement("p");
    item.className = "empty";
    item.textContent = message;
    return item;
  }
  async function loadFacesOverlay() {
    if (!faceList || facesLoaded) return;
    const fileId = openFacesButton?.dataset.facesItem || "";
    if (!fileId) return;
    faceList.replaceChildren(faceStatusMessage("Laster..."));
    try {
      const response = await csrfFetch(`/api/item-faces?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste ansikter.");
      faceList.innerHTML = payload.html || "";
      bindFaceAssignmentHandlers(faceList);
      facesLoaded = true;
    } catch (error) {
      faceList.replaceChildren(faceStatusMessage(error.message || "Kunne ikke laste ansikter."));
    }
  }
  async function openFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = false;
    await loadFacesOverlay();
    closeFacesButton?.focus();
  }
  function closeFacesOverlay() {
    if (!faceOverlay) return;
    faceOverlay.hidden = true;
  }
  function infoStatusRow(message) {
    const row = document.createElement("div");
    row.className = "info-row";
    const label = document.createElement("dt");
    label.textContent = "Status";
    const value = document.createElement("dd");
    value.textContent = message;
    row.append(label, value);
    return row;
  }
  async function loadInfoOverlay(fileId) {
    if (!infoList) return;
    if (infoLoaded && infoFileId === fileId) return;
    if (!fileId) return;
    infoLoaded = false;
    infoFileId = fileId;
    infoList.replaceChildren(infoStatusRow("Laster..."));
    try {
      const response = await csrfFetch(`/api/item-info?file_id=${encodeURIComponent(fileId)}`);
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke laste bildeinfo.");
      infoList.innerHTML = payload.html || "";
      infoLoaded = true;
    } catch (error) {
      infoList.replaceChildren(infoStatusRow(error.message || "Kunne ikke laste bildeinfo."));
    }
  }
  async function openInfoOverlay(opener) {
    if (!infoOverlay) return;
    infoOverlay.hidden = false;
    const fileId = opener?.dataset.infoItem || openInfoButtons[0]?.dataset.infoItem || "";
    await loadInfoOverlay(fileId);
    closeInfoButton?.focus();
  }
  function closeInfoOverlay() {
    if (!infoOverlay) return;
    infoOverlay.hidden = true;
  }
  function fitQuarterTurnMediaLink(img) {
    const link = img.closest(".media-link.quarter-turn");
    const stage = img.closest(".stage");
    if (!link || !stage || !img.naturalWidth || !img.naturalHeight) return;
    const stageRect = stage.getBoundingClientRect();
    const availableWidth = Math.max(stageRect.width, 1);
    const availableHeight = Math.max(stageRect.height, 1);
    const scale = Math.max(Math.min(availableWidth / img.naturalHeight, availableHeight / img.naturalWidth), 0.01);
    const originalWidth = img.naturalWidth * scale;
    const originalHeight = img.naturalHeight * scale;
    const rotation = img.dataset.viewRotation || "90";
    link.style.width = `${originalHeight}px`;
    link.style.height = `${originalWidth}px`;
    img.style.width = `${originalWidth}px`;
    img.style.height = `${originalHeight}px`;
    img.style.transform = `translate(-50%, -50%) rotate(${rotation}deg)`;
  }
  function fitQuarterTurnLegacyMedia(item) {
      const stage = item.closest(".stage");
      if (!stage) return;
      const img = item instanceof HTMLImageElement ? item : item.querySelector("img");
      if (!(img instanceof HTMLImageElement)) return;
      if (!img.naturalWidth || !img.naturalHeight) return;
      const stageRect = stage.getBoundingClientRect();
      const availableWidth = Math.max(stageRect.width, 1);
      const availableHeight = Math.max(stageRect.height, 1);
      const ratio = img.naturalWidth / img.naturalHeight;
      const maxOriginalWidth = Math.max(Math.min(availableHeight, availableWidth * ratio), 1);
      item.style.maxWidth = `${maxOriginalWidth}px`;
      item.style.maxHeight = "none";
  }
  function fitPersonFaceLayer(media) {
      const img = media.querySelector("img");
      const layer = media.querySelector(".person-face-layer");
      if (!(img instanceof HTMLImageElement) || !(layer instanceof HTMLElement)) return;
      if (!img.offsetWidth || !img.offsetHeight) return;
      layer.style.left = `${img.offsetLeft}px`;
      layer.style.top = `${img.offsetTop}px`;
      layer.style.width = `${img.offsetWidth}px`;
      layer.style.height = `${img.offsetHeight}px`;
  }
  function observePersonFaceLayers() {
    const mediaItems = document.querySelectorAll(".stage .person-media");
    if (!mediaItems.length) return;
    if ("ResizeObserver" in window) {
      const observer = new ResizeObserver(() => scheduleQuarterTurnFit());
      mediaItems.forEach(media => {
        const img = media.querySelector("img");
        observer.observe(media);
        if (img instanceof HTMLImageElement) observer.observe(img);
      });
    }
    mediaItems.forEach(media => {
      const img = media.querySelector("img");
      if (img instanceof HTMLImageElement && !img.complete) {
        img.addEventListener("load", scheduleQuarterTurnFit, {once: true});
      }
    });
  }
  function fitQuarterTurnMedia() {
    document.querySelectorAll('.stage .media-link.quarter-turn > img[data-view-rotation="90"], .stage .media-link.quarter-turn > img[data-view-rotation="270"]').forEach(img => {
      if (img instanceof HTMLImageElement) fitQuarterTurnMediaLink(img);
    });
    document.querySelectorAll('.stage .person-media[data-view-rotation="90"], .stage .person-media[data-view-rotation="270"]').forEach(fitQuarterTurnLegacyMedia);
    document.querySelectorAll(".stage .person-media").forEach(fitPersonFaceLayer);
  }
  function resetInlineMediaFit(img) {
    img.style.width = "";
    img.style.height = "";
    img.style.transform = "";
    const personMedia = img.closest(".person-media");
    if (personMedia instanceof HTMLElement) {
      personMedia.style.maxWidth = "";
      personMedia.style.maxHeight = "";
      personMedia.style.transform = "";
      personMedia.style.removeProperty("--quarter-turn-width");
    }
    const link = img.closest(".media-link");
    if (link instanceof HTMLElement) {
      link.style.width = "";
      link.style.height = "";
      link.classList.remove("quarter-turn");
    }
  }
  function applyViewRotation(rotation) {
    const normalizedRotation = Number(rotation) || 0;
    const img = document.querySelector(".stage .media-link > img, .stage .person-media img");
    if (!(img instanceof HTMLImageElement)) return;
    const link = img.closest(".media-link");
    const personMedia = img.closest(".person-media");
    resetInlineMediaFit(img);
    const rotationTarget = personMedia instanceof HTMLElement ? personMedia : img;
    rotationTarget.dataset.viewRotation = String(normalizedRotation);
    if (normalizedRotation === 0) {
      rotationTarget.removeAttribute("data-view-rotation");
      return;
    }
    rotationTarget.style.transform = `rotate(${normalizedRotation}deg)`;
    if ((normalizedRotation === 90 || normalizedRotation === 270) && link instanceof HTMLElement) {
      link.classList.add("quarter-turn");
    }
    if (normalizedRotation === 90 || normalizedRotation === 270) {
      scheduleQuarterTurnFit();
    } else if (personMedia instanceof HTMLElement) {
      fitPersonFaceLayer(personMedia);
    }
  }
  function scheduleQuarterTurnFit() {
    requestAnimationFrame(() => {
      fitQuarterTurnMedia();
      requestAnimationFrame(fitQuarterTurnMedia);
    });
  }
  function manualDateInput(name) {
    return manualDateForm?.querySelector(`[name="${name}"]`);
  }
  function selectedManualDateMode() {
    return manualDateInput("mode")?.checked ? manualDateInput("mode").value : manualDateForm?.querySelector('[name="mode"]:checked')?.value || "exact";
  }
  function setManualDateMode(mode) {
    manualDateForm?.querySelectorAll('[name="mode"]').forEach(input => {
      input.checked = input.value === mode;
    });
    updateManualDateFields();
  }
  function updateManualDateFields() {
    const mode = selectedManualDateMode();
    manualDateFields.forEach(label => {
      const field = label.dataset.manualDateField || "";
      const visible = ((mode === "exact" || mode === "uncertain") && field === "date") || (mode === "uncertain" && field === "uncertainty") || (mode === "between" && (field === "date_from" || field === "date_to"));
      label.hidden = !visible;
      label.querySelectorAll("input").forEach(input => {
        input.disabled = !visible;
      });
    });
  }
  function midpointIsoDate(dateFrom, dateTo) {
    if (!dateFrom || !dateTo) return "";
    const start = new Date(`${dateFrom}T00:00:00Z`);
    const end = new Date(`${dateTo}T00:00:00Z`);
    if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return dateFrom;
    return new Date(start.getTime() + Math.floor((end.getTime() - start.getTime()) / 2)).toISOString().slice(0, 10);
  }
  function openManualDateOverlay(button) {
    if (!manualDateOverlay || !manualDateForm) return;
    manualDateFileId = button.dataset.manualDateItem || "";
    const manualFrom = button.dataset.manualDateFrom || "";
    const manualTo = button.dataset.manualDateTo || "";
    const manualNote = button.dataset.manualDateNote || "";
    manualDateForm.reset();
    manualDateInput("date").value = midpointIsoDate(manualFrom, manualTo);
    manualDateInput("date_from").value = manualFrom;
    manualDateInput("date_to").value = manualTo;
    manualDateInput("note").value = manualNote;
    setManualDateMode(manualFrom && manualTo && manualFrom !== manualTo ? "between" : "exact");
    if (manualDateStatus) manualDateStatus.textContent = "";
    if (clearManualDateButton) clearManualDateButton.hidden = !(manualFrom && manualTo);
    manualDateOverlay.hidden = false;
    manualDateInput("date")?.focus();
  }
  function closeManualDateOverlay() {
    if (!manualDateOverlay) return;
    manualDateOverlay.hidden = true;
  }
  function openPersonRenameDialog(name) {
    if (!personRenameDialog || !personRenameForm || !personRenameNameInput || !personRenameOldNameInput) return;
    personRenameOldNameInput.value = name || "";
    personRenameNameInput.value = name || "";
    if (personRenameStatus) personRenameStatus.textContent = "";
    personRenameDialog.hidden = false;
    personRenameNameInput.focus();
    personRenameNameInput.select();
  }
  function closePersonRenameDialog() {
    if (!personRenameDialog) return;
    personRenameDialog.hidden = true;
  }
  function wireManualPersonRemoveButton(button) {
    button.addEventListener("click", async event => {
      event.preventDefault();
      event.stopPropagation();
      const fileId = Number(button.dataset.fileId);
      const personName = button.dataset.personName || "";
      if (!fileId || !personName) return;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/face-person-remove-file", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke fjerne person.");
        const pathParts = window.location.pathname.split("/").filter(Boolean);
        const currentPerson = pathParts[0] === "person" && pathParts[1] ? decodeURIComponent(pathParts[1]) : "";
        if (currentPerson === personName) {
          window.location.href = `/item/${fileId}`;
          return;
        }
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke fjerne person.");
        button.disabled = false;
      }
    });
  }
  function ensureRailPersonLink(name, url, confirmed = false, manual = false, fileId = null) {
    if (!name || !url) return;
    const tagRail = document.querySelector(".tag-rail");
    if (!tagRail) return;
    let section = Array.from(tagRail.querySelectorAll(".people-section")).find(item => {
      const heading = item.querySelector(".people-heading");
      return heading && heading.textContent === "Personer i bildet";
    });
    if (!section) {
      section = document.createElement("section");
      section.className = "people-section";
      const heading = document.createElement("h2");
      heading.className = "people-heading";
      heading.textContent = "Personer i bildet";
      section.append(heading);
      const before = tagRail.querySelector("[data-open-faces]") || tagRail.firstChild;
      tagRail.insertBefore(section, before);
    }
    let people = section.querySelector(".people");
    if (!people) {
      people = document.createElement("div");
      people.className = "people";
      section.append(people);
    }
    const exists = Array.from(people.querySelectorAll(".person-link")).some(link => link.dataset.personName === name);
    if (exists) return;
    const link = document.createElement("a");
    link.className = "person-link";
    link.href = url;
    link.dataset.personName = name;
    link.title = "Vis alle bilder med denne personen";
    link.append(document.createTextNode(name));
    if (confirmed) {
      const badge = document.createElement("span");
      badge.className = "confirmed-badge";
      badge.title = "Bekreftet";
      badge.setAttribute("aria-label", "Bekreftet");
      badge.textContent = " ✅";
      link.append(badge);
    }
    let item = link;
    if (manual && fileId) {
      const chip = document.createElement("span");
      chip.className = "manual-person-chip";
      const removeButton = document.createElement("button");
      removeButton.className = "manual-person-remove-button";
      removeButton.type = "button";
      removeButton.title = "Fjern manuell kobling til denne personen fra bildet";
      removeButton.setAttribute("aria-label", "Fjern manuell kobling til denne personen fra bildet");
      removeButton.dataset.manualPersonRemove = "";
      removeButton.dataset.fileId = String(fileId);
      removeButton.dataset.personName = name;
      removeButton.textContent = "×";
      wireManualPersonRemoveButton(removeButton);
      chip.append(link, removeButton);
      item = chip;
    }
    const addButton = people.querySelector("[data-open-manual-person-form]");
    people.insertBefore(item, addButton);
  }
  document.querySelectorAll('.stage img[data-view-rotation="90"], .stage img[data-view-rotation="270"], .stage .person-media[data-view-rotation="90"] img, .stage .person-media[data-view-rotation="270"] img').forEach(img => {
    if (img instanceof HTMLImageElement && !img.complete) {
      img.addEventListener("load", scheduleQuarterTurnFit, {once: true});
    }
  });
  observePersonFaceLayers();
  window.addEventListener("resize", scheduleQuarterTurnFit);
  scheduleQuarterTurnFit();
  openFacesButton?.addEventListener("click", openFacesOverlay);
  closeFacesButton?.addEventListener("click", closeFacesOverlay);
  openInfoButtons.forEach(button => {
    button.addEventListener("click", event => {
      event.preventDefault();
      openInfoOverlay(button);
    });
  });
  closeInfoButton?.addEventListener("click", closeInfoOverlay);
  closeManualDateButtons.forEach(button => {
    button.addEventListener("click", closeManualDateOverlay);
  });
  document.querySelectorAll("[data-open-manual-date]").forEach(button => {
    button.addEventListener("click", () => openManualDateOverlay(button));
  });
  manualDateForm?.querySelectorAll('[name="mode"]').forEach(input => {
    input.addEventListener("change", updateManualDateFields);
  });
  manualDateForm?.addEventListener("submit", async event => {
    event.preventDefault();
    if (!manualDateFileId) return;
    if (manualDateStatus) manualDateStatus.textContent = "Lagrer...";
    manualDateForm.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await csrfFetch("/api/item-manual-date", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          file_id: Number(manualDateFileId),
          mode: selectedManualDateMode(),
          date: manualDateInput("date")?.value || "",
          uncertainty: manualDateInput("uncertainty")?.value || "",
          date_from: manualDateInput("date_from")?.value || "",
          date_to: manualDateInput("date_to")?.value || "",
          note: manualDateInput("note")?.value || "",
        }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke lagre dato.");
      window.location.reload();
    } catch (error) {
      if (manualDateStatus) manualDateStatus.textContent = error.message || "Kunne ikke lagre dato.";
      manualDateForm.querySelectorAll("button, input").forEach(item => item.disabled = false);
      updateManualDateFields();
    }
  });
  clearManualDateButton?.addEventListener("click", async () => {
    if (!manualDateFileId) return;
    if (!confirm("Fjerne manuell dato fra bildet?")) return;
    if (manualDateStatus) manualDateStatus.textContent = "Fjerner...";
    manualDateForm?.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await csrfFetch("/api/item-manual-date-clear", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_id: Number(manualDateFileId)}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke fjerne dato.");
      window.location.reload();
    } catch (error) {
      if (manualDateStatus) manualDateStatus.textContent = error.message || "Kunne ikke fjerne dato.";
      manualDateForm?.querySelectorAll("button, input").forEach(item => item.disabled = false);
      updateManualDateFields();
    }
  });
  searchForm?.addEventListener("submit", () => {
    if (searchForm.dataset.modelLoaded === "true") return;
    if (searchLoading) searchLoading.hidden = false;
  });
  let searchPreloadStarted = false;
  function preloadSearchModel() {
    if (searchPreloadStarted) return;
    searchPreloadStarted = true;
    fetch("/api/search-preload", {keepalive: true}).catch(() => {});
  }
  document.querySelectorAll("[data-search-preload]").forEach(link => {
    link.addEventListener("pointerdown", preloadSearchModel);
    link.addEventListener("click", preloadSearchModel);
  });
  closePersonRenameButton?.addEventListener("click", closePersonRenameDialog);
  document.querySelectorAll("[data-open-person-rename]").forEach(button => {
    button.addEventListener("click", () => openPersonRenameDialog(button.dataset.personName || ""));
  });
  document.querySelectorAll("[data-delete-person-name]").forEach(button => {
    button.addEventListener("click", async () => {
      const personName = button.dataset.deletePersonName || "";
      if (!personName) return;
      const command = `bildebank face-person-delete "${personName}"`;
      if (!confirm(`Slette personen ${personName} fra ansiktsdatabasen?\n\nDette sletter bekreftede ansiktskoblinger og forslag for personen, men ingen bilder.\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/face-person-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke slette person.");
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke slette person.");
        button.disabled = false;
      }
    });
  });
  personRenameDialog?.addEventListener("click", event => {
    if (event.target === personRenameDialog) closePersonRenameDialog();
  });
  personRenameForm?.addEventListener("submit", async event => {
    event.preventDefault();
    const oldName = personRenameOldNameInput?.value || "";
    const newName = personRenameNameInput?.value?.trim() || "";
    if (personRenameStatus) personRenameStatus.textContent = "Lagrer...";
    personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await csrfFetch("/api/face-person-rename", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({old_name: oldName, new_name: newName}),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke endre navn.");
      window.location.reload();
    } catch (error) {
      if (personRenameStatus) personRenameStatus.textContent = error.message || "Kunne ikke endre navn.";
      personRenameForm.querySelectorAll("button, input").forEach(item => item.disabled = false);
      personRenameNameInput?.focus();
    }
  });
  document.querySelectorAll("[data-rotate-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.rotateItem);
      const direction = button.dataset.rotateDirection || "";
      const itemRoot = button.closest("[data-browser-item-id]");
      const requestBody = {file_id: fileId, direction};
      if (itemRoot?.dataset.browserSourceUrl) requestBody.source_url = itemRoot.dataset.browserSourceUrl;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/item-rotate", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(requestBody),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke rotere.");
        if (payload.redirect_url) {
          window.location.href = payload.redirect_url;
          return;
        }
        applyViewRotation(payload.rotation);
        button.disabled = false;
      } catch (error) {
        alert(error.message || "Kunne ikke rotere.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-tag-toggle]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.tagToggle);
      const tagName = button.dataset.tagName || "";
      const tagged = button.getAttribute("aria-pressed") !== "true";
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/item-tag", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, tag_name: tagName, tagged}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre tagg.");
        const encodedTag = encodeURIComponent(payload.tag_name || tagName);
        const hideRedirect = button.dataset.tagHideRedirect || "";
        if (payload.tagged && hideRedirect && (payload.tag_name || tagName) === "Ute av fokus") {
          window.location.href = hideRedirect;
          return;
        }
        if (!payload.tagged && window.location.pathname.startsWith(`/tag/${encodedTag}/`)) {
          window.location.href = `/tag/${encodedTag}`;
          return;
        }
        button.setAttribute("aria-pressed", payload.tagged ? "true" : "false");
        button.classList.toggle("active", Boolean(payload.tagged));
        button.disabled = false;
      } catch (error) {
        alert(error.message || "Kunne ikke lagre tagg.");
        button.disabled = false;
      }
    });
  });
  function updateHotkeyForm(form) {
    const action = form.querySelector("[data-hotkey-action]")?.value || "";
    form.querySelectorAll("[data-hotkey-fields]").forEach(group => {
      const visible = group.dataset.hotkeyFields === action;
      group.hidden = !visible;
      group.querySelectorAll("input, select, textarea, button").forEach(input => {
        input.disabled = !visible;
      });
    });
  }
  document.querySelectorAll(".hotkey-form").forEach(form => {
    updateHotkeyForm(form);
    form.querySelector("[data-hotkey-action]")?.addEventListener("change", () => updateHotkeyForm(form));
  });
  async function applyHotkeyAction(key) {
    const itemRoot = document.querySelector("[data-browser-item-id]");
    const fileId = Number(itemRoot?.dataset.browserItemId);
    const hotkeysEnabled = itemRoot?.dataset.browserHotkeysEnabled === "true";
    if (!fileId || !hotkeysEnabled || !["1", "2", "3", "4", "5"].includes(key)) return false;
    const requestBody = {file_id: fileId, key};
    if (itemRoot?.dataset.browserSourceUrl) requestBody.source_url = itemRoot.dataset.browserSourceUrl;
    try {
      const response = await csrfFetch("/api/item-hotkey-action", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(requestBody),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke utføre hurtigtast.");
      if (payload.redirect_url) {
        window.location.href = payload.redirect_url;
        return true;
      }
      if (payload.action === "tag" && payload.tagged && payload.tag_name === "Ute av fokus") {
        const matchingTagButton = Array.from(document.querySelectorAll("[data-tag-toggle]")).find(button => {
          return (button.dataset.tagName || "") === payload.tag_name && button.dataset.tagHideRedirect;
        });
        if (matchingTagButton) {
          window.location.href = matchingTagButton.dataset.tagHideRedirect;
          return true;
        }
      }
      window.location.reload();
    } catch (error) {
      alert(error.message || "Kunne ikke utføre hurtigtast.");
    }
    return true;
  }
  async function removeManualLocation(button) {
    if (!button || button.disabled) return;
    const fileId = Number(button.dataset.removeManualLocationItem);
    if (!fileId) return;
    if (!confirm("Fjerne manuell H3-lokasjon fra bildet?")) return;
    button.disabled = true;
    try {
      const response = await csrfFetch("/api/item-manual-location-remove", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({file_id: fileId}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke fjerne manuelt sted.");
      window.location.reload();
    } catch (error) {
      alert(error.message || "Kunne ikke fjerne manuelt sted.");
      button.disabled = false;
    }
  }
  document.querySelectorAll("[data-remove-manual-location-item]").forEach(button => {
    button.addEventListener("click", () => removeManualLocation(button));
  });
  document.querySelectorAll("[data-delete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.deleteItem);
      const path = button.dataset.deletePath || "";
      const redirectUrl = button.dataset.deleteRedirect || "/";
      if (!confirm(`Flytte til deleted/?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/item-delete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke slette.");
        window.location.href = redirectUrl;
      } catch (error) {
        alert(error.message || "Kunne ikke slette.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-undelete-item]").forEach(button => {
    button.addEventListener("click", async () => {
      const fileId = Number(button.dataset.undeleteItem);
      const path = button.dataset.undeletePath || "";
      if (!confirm(`Flytte tilbake til bildesamlingen?\n\n${path}`)) return;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/item-undelete", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke angre sletting.");
        button.closest(".removed-row")?.remove();
      } catch (error) {
        alert(error.message || "Kunne ikke angre sletting.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-unconfirm-face]").forEach(button => {
    button.addEventListener("click", async () => {
      const faceId = Number(button.dataset.unconfirmFace);
      const personName = button.dataset.unconfirmPerson || "";
      if (!faceId || !personName) return;
      const command = `bildebank face-person-remove-face "${personName}" ${faceId}`;
      if (!confirm(`Avbekrefte face-id ${faceId} fra ${personName}?\n\nTilsvarer:\n${command}`)) return;
      button.disabled = true;
      try {
        const response = await csrfFetch("/api/face-person-remove-face", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({face_id: faceId, person_name: personName}),
        });
        const payload = await response.json();
        if (!payload.ok) throw new Error(payload.error || "Kunne ikke avbekrefte.");
        if (payload.redirect_url) {
          window.location.href = payload.redirect_url;
          return;
        }
        window.location.reload();
      } catch (error) {
        alert(error.message || "Kunne ikke avbekrefte.");
        button.disabled = false;
      }
    });
  });
  document.querySelectorAll("[data-manual-person-form]").forEach(form => {
    const select = form.querySelector('select[name="person_name"]');
    const status = form.querySelector("[data-manual-person-status]");
    const fileId = Number(form.dataset.fileId);
    const section = form.closest(".people-section");
    section?.querySelector("[data-open-manual-person-form]")?.addEventListener("click", () => {
      section.classList.add("manual-person-editing");
      form.hidden = false;
      if (status) status.textContent = "";
      select?.focus();
    });
    form.querySelector("[data-close-manual-person-form]")?.addEventListener("click", () => {
      section?.classList.remove("manual-person-editing");
      form.hidden = true;
      if (status) status.textContent = "";
    });
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const personName = select?.value || "";
      if (!fileId || !personName) return;
      if (status) status.textContent = "Lagrer...";
      form.querySelectorAll("button, select").forEach(item => item.disabled = true);
      try {
        const response = await csrfFetch("/api/face-person-add-file", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({file_id: fileId, person_name: personName}),
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || "Kunne ikke lagre person.");
        ensureRailPersonLink(payload.person_name, payload.person_url, true, true, fileId);
        if (status) status.textContent = "Lagret.";
        form.querySelectorAll("button, select").forEach(item => item.disabled = false);
      } catch (error) {
        if (status) status.textContent = error.message || "Kunne ikke lagre person.";
        form.querySelectorAll("button, select").forEach(item => item.disabled = false);
      }
    });
  });
  document.querySelectorAll("[data-manual-person-remove]").forEach(button => {
    wireManualPersonRemoveButton(button);
  });
  faceOverlay?.addEventListener("click", event => {
    if (event.target === faceOverlay || event.target.classList?.contains("lightbox-stage")) closeFacesOverlay();
  });
  infoOverlay?.addEventListener("click", event => {
    if (event.target === infoOverlay) closeInfoOverlay();
  });
  async function assignFace(detail, status, endpoint, faceId, personName) {
    if (!detail || !status || !faceId || !personName) return;
    status.textContent = "Lagrer...";
    detail.querySelectorAll("button, input").forEach(item => item.disabled = true);
    try {
      const response = await csrfFetch(endpoint, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({face_id: Number(faceId), person_name: personName}),
      });
      const payload = await response.json();
      if (!payload.ok) throw new Error(payload.error || "Kunne ikke lagre.");
      status.textContent = `Koblet til ${payload.person_name}.`;
      ensureRailPersonLink(payload.person_name, payload.person_url, payload.confirmed);
      detail.remove();
      if (!document.querySelector(".face-detail")) {
        closeFacesOverlay();
        window.location.reload();
      }
    } catch (error) {
      status.textContent = error.message || "Kunne ikke lagre.";
      detail.querySelectorAll("button, input").forEach(item => item.disabled = false);
    }
  }
  function bindFaceAssignmentHandlers(root = document) {
    root.querySelectorAll(".assign-person-button").forEach(button => {
      button.addEventListener("click", async () => {
        const faceId = button.dataset.faceId;
        const personName = button.dataset.personName;
        const detail = button.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        await assignFace(detail, status, "/api/face-person-add-face", faceId, personName);
      });
    });
    root.querySelectorAll("[data-new-person-form]").forEach(form => {
      form.addEventListener("submit", async event => {
        event.preventDefault();
        const detail = form.closest(".face-detail");
        const status = detail?.querySelector(".assign-status");
        const faceId = form.querySelector('input[name="face_id"]')?.value;
        const personName = form.querySelector('input[name="person_name"]')?.value?.trim();
        await assignFace(detail, status, "/api/face-person-create-and-add-face", faceId, personName);
      });
    });
  }
  function attachSwipeNavigation(container, onSwipe) {
    if (!container) return;
    const minDistance = 40;
    const maxTapDrift = 10;
    const verticalDominanceRatio = 0.75;
    let start = null;
    let suppressNextClick = false;
    function startSwipe(x, y, pointerId = null) {
      start = {x, y, pointerId};
    }
    function finishSwipe(x, y, pointerId = null) {
      if (!start) return false;
      if (start.pointerId !== null && pointerId !== null && start.pointerId !== pointerId) return false;
      const dx = x - start.x;
      const dy = y - start.y;
      start = null;
      const absX = Math.abs(dx);
      const absY = Math.abs(dy);
      if (absX <= maxTapDrift && absY <= maxTapDrift) return false;
      if (absX < minDistance || absX <= absY * verticalDominanceRatio) return false;
      suppressNextClick = true;
      onSwipe(dx < 0 ? 1 : -1);
      return true;
    }
    container.addEventListener("click", event => {
      if (!suppressNextClick) return;
      suppressNextClick = false;
      event.preventDefault();
      event.stopPropagation();
    }, true);
    if (window.PointerEvent) {
      container.addEventListener("pointerdown", event => {
        if (event.pointerType !== "touch" && event.pointerType !== "pen") return;
        try {
          container.setPointerCapture(event.pointerId);
        } catch {
          // Some WebKit versions throw when capture is not available for this pointer.
        }
        startSwipe(event.clientX, event.clientY, event.pointerId);
      });
      container.addEventListener("pointerup", event => {
        if (event.pointerType !== "touch" && event.pointerType !== "pen") return;
        if (finishSwipe(event.clientX, event.clientY, event.pointerId)) event.preventDefault();
      });
      container.addEventListener("pointercancel", () => {
        start = null;
      });
      return;
    }
    container.addEventListener("touchstart", event => {
      if (event.changedTouches.length !== 1) return;
      const touch = event.changedTouches[0];
      startSwipe(touch.clientX, touch.clientY);
    }, {passive: true});
    container.addEventListener("touchend", event => {
      if (event.changedTouches.length !== 1) return;
      const touch = event.changedTouches[0];
      if (finishSwipe(touch.clientX, touch.clientY)) event.preventDefault();
    }, {passive: false});
    container.addEventListener("touchcancel", () => {
      start = null;
    }, {passive: true});
  }
  attachSwipeNavigation(document.querySelector(".stage"), direction => {
    const selector = direction > 0 ? '[data-key-nav="next"]' : '[data-key-nav="previous"]';
    const link = document.querySelector(selector);
    if (link instanceof HTMLAnchorElement) window.location.href = link.href;
  });
  bindFaceAssignmentHandlers();
  document.addEventListener("keydown", event => {
    if (faceOverlay && !faceOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeFacesOverlay();
      }
      return;
    }
    if (infoOverlay && !infoOverlay.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeInfoOverlay();
      }
      return;
    }
    if (personRenameDialog && !personRenameDialog.hidden) {
      if (event.key === "Escape") {
        event.preventDefault();
        closePersonRenameDialog();
      }
      return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLButtonElement ||
      target?.isContentEditable
    ) return;
    if (["1", "2", "3", "4", "5"].includes(event.key)) {
      const itemRoot = document.querySelector("[data-browser-item-id]");
      if (itemRoot?.dataset.browserHotkeysEnabled !== "true") return;
      event.preventDefault();
      applyHotkeyAction(event.key);
      return;
    }
    const selector = {
      ArrowLeft: '[data-key-nav="previous"]',
      ArrowRight: '[data-key-nav="next"]',
      ArrowUp: '[data-key-nav="previous-month"]',
      ArrowDown: '[data-key-nav="next-month"]',
      PageUp: '[data-key-nav="previous-year"]',
      PageDown: '[data-key-nav="next-year"]',
    }[event.key] || "";
    if (!selector) return;
    const link = document.querySelector(selector);
    if (!(link instanceof HTMLAnchorElement)) return;
    event.preventDefault();
    window.location.href = link.href;
  });
