// Visibility rules for form parameters: a parameter listed here is shown (and sent) only when its
// function returns true. Parameters not listed are always shown.
//
// Each rule receives `ui`:
//   ui.backend               the chosen backend from /ui/backends.json, e.g.
//                            { backend_type: "foil", defaults: { spreadsheet_mode: "auto", ... } }
//                            ({ defaults: {} } for routes without a backend, like auto or aliases)
//   ui.value(name)           current value of another parameter ("" when left at "(default)")
//   ui.isShown(name)         whether another parameter is currently shown
//   ui.fileHasExtension([])  whether the selected file has one of these extensions
//   ui.file                  the selected File, or null
(function () {
  "use strict";

  const SPREADSHEET_EXTENSIONS = ["xls", "xlsx", "ods"];

  window.MarkgateParamRules = {
    // Only foil backends use it, and only for spreadsheet files
    spreadsheet_mode: (ui) =>
      ui.backend.backend_type === "foil" && ui.fileHasExtension(SPREADSHEET_EXTENSIONS),

    // Only used by the "auto" spreadsheet mode; "(default)" means the backend's configured mode
    excel_min_output_ratio: (ui) =>
      ui.isShown("spreadsheet_mode") &&
      (ui.value("spreadsheet_mode") || ui.backend.defaults.spreadsheet_mode) === "auto",
  };
})();
