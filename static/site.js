(function () {
  const config = window.MEMORY_HOUSE || {};
  const maxFileSizeBytes = Number(config.maxFileSizeBytes || 0);
  const maxBatchBytes = Number(config.maxBatchBytes || 0);

  const body = document.body;
  const menuToggle = document.querySelector("[data-menu-toggle]");
  const topMenu = document.getElementById("topMenu");
  const uploadModal = document.getElementById("uploadModal");
  const lightbox = document.getElementById("lightbox");
  const lightboxImage = document.getElementById("lightboxImage");
  const lightboxCaption = document.getElementById("lightboxCaption");
  const uploadForm = document.getElementById("uploadForm");
  const fileInput = document.getElementById("fileInput");
  const fileChosen = document.getElementById("fileChosen");

  function setBodyLock() {
    const menuOpen = topMenu && topMenu.classList.contains("active") && window.innerWidth <= 820;
    const modalOpen = uploadModal && !uploadModal.hasAttribute("hidden");
    const lightboxOpen = lightbox && !lightbox.hasAttribute("hidden");
    body.classList.toggle("modal-open", Boolean(menuOpen || modalOpen || lightboxOpen));
  }

  function closeMenu() {
    if (!topMenu || !menuToggle) {
      return;
    }
    topMenu.classList.remove("active");
    menuToggle.setAttribute("aria-expanded", "false");
    setBodyLock();
  }

  function openModal(modal) {
    if (!modal) {
      return;
    }
    modal.removeAttribute("hidden");
    closeMenu();
    setBodyLock();
  }

  function closeModal(modal) {
    if (!modal) {
      return;
    }
    modal.setAttribute("hidden", "");
    setBodyLock();
  }

  function openLightbox(url, caption) {
    if (!lightbox || !lightboxImage || !lightboxCaption) {
      return;
    }
    lightboxImage.src = url || "";
    lightboxImage.alt = caption || "Saved memory";
    lightboxCaption.textContent = caption || "A favorite frame worth keeping close.";
    lightbox.removeAttribute("hidden");
    setBodyLock();
  }

  function closeLightbox() {
    if (!lightbox) {
      return;
    }
    lightbox.setAttribute("hidden", "");
    if (lightboxImage) {
      lightboxImage.src = "";
      lightboxImage.alt = "";
    }
    if (lightboxCaption) {
      lightboxCaption.textContent = "";
    }
    setBodyLock();
  }

  function listFiles(files) {
    const values = Array.from(files || []);
    if (!fileChosen) {
      return;
    }
    if (!values.length) {
      fileChosen.textContent = "";
      return;
    }
    if (values.length === 1) {
      fileChosen.textContent = values[0].name;
      return;
    }
    const preview = values.slice(0, 2).map((file) => file.name).join(", ");
    const suffix = values.length > 2 ? ` + ${values.length - 2} more` : "";
    fileChosen.textContent = `${preview}${suffix}`;
  }

  function validateFiles(files) {
    const values = Array.from(files || []);
    if (!values.length) {
      return true;
    }

    const oversizedCount = values.filter((file) => file.size > maxFileSizeBytes).length;
    const totalSize = values.reduce((sum, file) => sum + file.size, 0);

    if (oversizedCount) {
      const maxMb = Math.floor(maxFileSizeBytes / (1024 * 1024));
      window.alert(`${oversizedCount} file(s) are too large. Keep each photo under ${maxMb}MB.`);
      return false;
    }

    if (maxBatchBytes && totalSize > maxBatchBytes) {
      const maxMb = Math.floor(maxBatchBytes / (1024 * 1024));
      window.alert(`This batch is too large. Keep the full upload under ${maxMb}MB.`);
      return false;
    }

    return true;
  }

  function bindOpeners() {
    document.querySelectorAll("[data-open-modal]").forEach((button) => {
      button.addEventListener("click", () => {
        const modal = document.getElementById(button.dataset.openModal);
        openModal(modal);
      });
    });

    document.querySelectorAll("[data-close-modal]").forEach((button) => {
      button.addEventListener("click", () => closeModal(uploadModal));
    });

    document.querySelectorAll("[data-close-lightbox]").forEach((button) => {
      button.addEventListener("click", closeLightbox);
    });
  }

  function bindOverlayClose() {
    if (uploadModal) {
      uploadModal.addEventListener("click", (event) => {
        if (event.target === uploadModal) {
          closeModal(uploadModal);
        }
      });
    }

    if (lightbox) {
      lightbox.addEventListener("click", (event) => {
        if (event.target === lightbox) {
          closeLightbox();
        }
      });
    }
  }

  function bindMenu() {
    if (!menuToggle || !topMenu) {
      return;
    }
    menuToggle.addEventListener("click", () => {
      const nextState = !topMenu.classList.contains("active");
      topMenu.classList.toggle("active", nextState);
      menuToggle.setAttribute("aria-expanded", String(nextState));
      setBodyLock();
    });

    topMenu.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", closeMenu);
    });
  }

  function bindLightboxTriggers() {
    document.querySelectorAll("[data-lightbox-url]").forEach((element) => {
      const openFromElement = () => {
        openLightbox(
          element.getAttribute("data-lightbox-url"),
          element.getAttribute("data-lightbox-caption")
        );
      };

      element.addEventListener("click", (event) => {
        if (event.target.closest("form, button, input, textarea, select, a")) {
          return;
        }
        openFromElement();
      });

      element.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        event.preventDefault();
        openFromElement();
      });
    });
  }

  function bindFileInput() {
    if (!fileInput) {
      return;
    }

    fileInput.addEventListener("change", () => {
      const files = Array.from(fileInput.files || []);
      if (!validateFiles(files)) {
        fileInput.value = "";
        listFiles([]);
        return;
      }
      listFiles(files);
    });

    const dropzone = fileInput.closest(".dropzone");
    if (!dropzone) {
      return;
    }

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });

    dropzone.addEventListener("drop", (event) => {
      const files = event.dataTransfer ? event.dataTransfer.files : null;
      if (!files || !files.length) {
        return;
      }
      try {
        fileInput.files = files;
      } catch (_error) {
        // Some browsers do not allow assigning FileList directly.
      }
      if (!validateFiles(files)) {
        fileInput.value = "";
        listFiles([]);
        return;
      }
      listFiles(files);
    });
  }

  function bindReplaceButtons() {
    document.querySelectorAll("[data-replace-trigger]").forEach((button) => {
      button.addEventListener("click", () => {
        const form = button.closest("form");
        const input = form ? form.querySelector(".replace-input") : null;
        if (input) {
          input.click();
        }
      });
    });

    document.querySelectorAll(".replace-input").forEach((input) => {
      input.addEventListener("change", () => {
        const file = input.files && input.files[0];
        if (!file) {
          return;
        }
        if (!validateFiles([file])) {
          input.value = "";
          return;
        }
        if (!window.confirm("Replace this featured photo?")) {
          input.value = "";
          return;
        }
        input.form.submit();
      });
    });
  }

  function bindConfirmForms() {
    document.querySelectorAll("form[data-confirm]").forEach((form) => {
      form.addEventListener("submit", (event) => {
        const message = form.getAttribute("data-confirm") || "Continue?";
        if (!window.confirm(message)) {
          event.preventDefault();
        }
      });
    });
  }

  function bindUploadSubmit() {
    if (!uploadForm || !fileInput) {
      return;
    }

    uploadForm.addEventListener("submit", async (event) => {
      const files = Array.from(fileInput.files || []);
      if (!files.length) {
        return;
      }
      if (!validateFiles(files)) {
        event.preventDefault();
        return;
      }
      if (files.length === 1) {
        return;
      }

      event.preventDefault();

      const submitButton = uploadForm.querySelector('button[type="submit"]');
      const captionInput = uploadForm.querySelector('input[name="caption"]');
      const dateInput = uploadForm.querySelector('input[name="moment_date"]');
      const sectionInput = uploadForm.querySelector('select[name="section"]');
      const originalLabel = submitButton ? submitButton.textContent : "";

      if (submitButton) {
        submitButton.disabled = true;
        submitButton.textContent = "Saving...";
      }

      try {
        for (const file of files) {
          const formData = new FormData();
          formData.append("caption", captionInput ? captionInput.value : "");
          formData.append("moment_date", dateInput ? dateInput.value : "");
          formData.append("section", sectionInput ? sectionInput.value : "gallery");
          formData.append("_async", "1");
          formData.append("photo", file);

          const response = await fetch("/upload", {
            method: "POST",
            body: formData,
          });

          if (!response.ok) {
            throw new Error(`Upload failed with status ${response.status}`);
          }
        }

        window.location.reload();
      } catch (error) {
        console.error(error);
        window.alert("One or more uploads failed. Smaller batches usually work best online.");
      } finally {
        if (submitButton) {
          submitButton.disabled = false;
          submitButton.textContent = originalLabel || "Save photo";
        }
      }
    });
  }

  function bindKeyboardShortcuts() {
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeModal(uploadModal);
        closeLightbox();
        closeMenu();
      }
    });

    window.addEventListener("resize", () => {
      if (window.innerWidth > 820) {
        closeMenu();
      }
    });
  }

  function bindRevealAnimations() {
    const items = document.querySelectorAll(".reveal");
    if (!items.length) {
      return;
    }
    if (!("IntersectionObserver" in window)) {
      items.forEach((item) => item.classList.add("is-visible"));
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) {
            return;
          }
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        });
      },
      {
        threshold: 0.16,
        rootMargin: "0px 0px -40px 0px",
      }
    );

    items.forEach((item) => observer.observe(item));
  }

  bindOpeners();
  bindOverlayClose();
  bindMenu();
  bindLightboxTriggers();
  bindFileInput();
  bindReplaceButtons();
  bindConfirmForms();
  bindUploadSubmit();
  bindKeyboardShortcuts();
  bindRevealAnimations();
  setBodyLock();
})();
