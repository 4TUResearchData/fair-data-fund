function perform_upload (files, current_file, dataset_uuid) {
    let total_files = files.length;
    let index = current_file - 1;
    let data  = new FormData();

    if (files[index] === undefined || files[index] == null) {
        show_message ("failure", "<p>Uploading file(s) failed due to a web browser incompatibility.</p>");
        jQuery("#file-upload h4").text("Uploading failed.");
        return;
    } else if (files[index].webkitRelativePath !== undefined &&
               files[index].webkitRelativePath != "") {
        data.append ("file", files[index], files[index].webkitRelativePath);
    } else if (files[index].name !== undefined) {
        data.append ("file", files[index], files[index].name);
    } else {
        jQuery("#file-upload h4").text("Click here to open file dialog");
        jQuery("#file-upload p").text("Because the drag and drop functionality"+
                                      " does not work for your web browser.");
        show_message ("failure", "<p>Uploading file(s). Please try selecting " +
                                 "files with the file chooser instead of " +
                                 "using the drag-and-drop.</p>");
        return;
    }

    jQuery.ajax({
        xhr: function () {
            let xhr = new window.XMLHttpRequest();
            xhr.upload.addEventListener("progress", function (evt) {
                if (evt.lengthComputable) {
                    let completed = parseInt(evt.loaded / evt.total * 100);
                    jQuery("#file-upload h4").text(`Uploading at ${completed}% (${current_file}/${total_files})`);
                    if (completed === 100) {
                        jQuery("#file-upload h4").text(`Computing MD5 ... (${current_file}/${total_files})`);
                    }
                }
            }, false);
            return xhr;
        },
        url:         `/v3/datasets/${dataset_uuid}/upload`,
        type:        "POST",
        data:        data,
        processData: false,
        contentType: false
    }).done(function () {
        jQuery("#file-upload h4").text("Drag files here");
        if (current_file < total_files) {
            return perform_upload (files, current_file + 1, dataset_uuid);
        } else {
            render_files_for_dataset (dataset_uuid, null);
        }
    }).fail(function () {
        show_message ("failure", "<p>Uploading file(s) failed.</p>");
    });
}

function radio_button_value (name) {
    let item = jQuery(`input[name='${name}']:checked`)[0];
    if (item !== undefined) { item = item["value"]; }
    else { item = null; }
    return item;
}

function gather_form_data (application_uuid) {
    return {
        "uuid":          application_uuid,
        "name":          or_null(jQuery("#name").val()),
        "pronouns":      or_null(jQuery("#pronouns").val()),
        "institution":   or_null(jQuery("#institution").val()),
        "faculty":       or_null(jQuery("#faculty").val()),
        "department":    or_null(jQuery("#department").val()),
        "position":      or_null(jQuery("#position").val()),
        "discipline":    or_null(jQuery("#discipline").val()),
        "datatype":      or_null(jQuery("#datatype").val()),
        "description":   or_null(jQuery("#description .ql-editor").html()),
        "size":          or_null(jQuery("#size").val()),
        "whodoesit":     or_null(jQuery("#whodoesit .ql-editor").html()),
        "achievement":   or_null(jQuery("#achievement .ql-editor").html()),
        "fair_summary":  or_null(jQuery("#fair_summary .ql-editor").html()),
        "findable":      or_null(jQuery("#findable .ql-editor").html()),
        "accessible":    or_null(jQuery("#accessible .ql-editor").html()),
        "interoperable": or_null(jQuery("#interoperable .ql-editor").html()),
        "reusable":      or_null(jQuery("#reusable .ql-editor").html()),
        "summary":       or_null(jQuery("#summary .ql-editor").html()),
        "data_timing":   radio_button_value ("data-timing"),
        "refinement":    radio_button_value ("refinement-needed")
    }
}

function save_draft (application_uuid, event, notify=true, on_success=jQuery.noop) {
    if (event !== null) {
        event.preventDefault();
        event.stopPropagation();
    }
    form_data = gather_form_data (application_uuid);
    jQuery.ajax({
        url:         `/application-form/${application_uuid}`,
        type:        "PUT",
        contentType: "application/json",
        accept:      "application/json",
        data:        JSON.stringify(form_data),
    }).done(function () {
        if (notify) {
            show_message ("success", "<p>Saved changes.</p>");
        }
        on_success ();
    }).fail(function () {
        if (notify) {
            show_message ("failure", "<p>Failed to save draft. Please try again at a later time.</p>");
        }
    });
}

function submit (application_uuid, event, notify=true, on_success=jQuery.noop) {
    if (event !== null) {
        event.preventDefault();
        event.stopPropagation();
    }
    form_data = gather_form_data (application_uuid);
    jQuery.ajax({
        url:         `/application-form/${application_uuid}/submit`,
        type:        "PUT",
        contentType: "application/json",
        accept:      "application/json",
        data:        JSON.stringify(form_data),
    }).done(function (data, textStatus) {
        on_success ();
        window.location.replace (`${data.redirect_to}`);
    }).fail(function () {
        if (notify) {
            show_message ("failure", "<p>Failed to submit the form. Please try again at a later time.</p>");
        }
    });
}

jQuery(document).ready(function (){
    new Quill('#description', { theme: '4tu' });
    new Quill('#whodoesit', { theme: '4tu' });
    new Quill('#achievement', { theme: '4tu' });
    new Quill('#findable', { theme: '4tu' });
    new Quill('#accessible', { theme: '4tu' });
    new Quill('#interoperable', { theme: '4tu' });
    new Quill('#reusable', { theme: '4tu' });
    new Quill('#summary', { theme: '4tu' });
    new Quill('#fair_summary', { theme: '4tu' });
    //new Quill('.texteditor', { theme: '4tu' });

    var budgetUploader = new Dropzone("#budget-dropzone", {
        url:               `/application-form/${application_uuid}/upload-budget`,
        paramName:         "file",
        maxFilesize:       1000000,
        maxFiles:          1000000,
        parallelUploads:   1,
        autoProcessQueue:  false,
        autoQueue:         true,
        ignoreHiddenFiles: false,
        disablePreviews:   false,
        init: function() {
            $(window).on('beforeunload', function() {
                if (budgetUploader.getUploadingFiles().length > 0 ||
                    budgetUploader.getQueuedFiles().length > 0) {
                    // Custom message cannot be used in most browsers
                    // since it was used for scam. Therefore, pop-up message
                    // depends on user's browser.
                    return 1;
                }
            });
        },
        accept: function(file, done) {
            done();
            budgetUploader.processQueue();
        },
        complete: function (file) {
            if (budgetUploader.getUploadingFiles().length === 0 &&
                budgetUploader.getQueuedFiles().length === 0) {
                let rejected_files = budgetUploader.getRejectedFiles();
                for (let rejected of rejected_files) {
                    if (rejected.status == "error") {
                        show_message ("failure", `<p>Failed to upload '${rejected.upload.filename}'.</p>`);
                    }
                }
                render_files_for_dataset (dataset_uuid, budgetUploader);
            } else {
                budgetUploader.processQueue();
            }
            budgetUploader.removeFile(file);
        },
        error: function(file, message) {
            show_message ("failure",
                          (`<p>Failed to upload ${file.upload.filename}:` +
                           ` ${message.message}</p>`));
        }
    });

    jQuery("#save").on("click", function (event)   { save_draft (application_uuid, event); });
    jQuery("#submit").on("click", function (event) { submit (application_uuid, event); });
});
