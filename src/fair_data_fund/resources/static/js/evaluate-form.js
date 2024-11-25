function gather_form_data () {
    return {
        "citation": jQuery("#score-citation").val(),
        "datatypes": jQuery("#score-datatypes").val(),
        "budget": jQuery("#score-budget").val(),
        "other": jQuery("#score-other").val(),
    };
}

function submit_application (application_uuid) {
    form_data = gather_form_data();
    jQuery.ajax({
        url:         `/review/${application_uuid}`,
        type:        "PUT",
        contentType: "application/json",
        accept:      "application/json",
        data:        JSON.stringify (form_data)
    }).done(function () {
        window.location.replace("/review/dashboard");
    }).fail(function () {
        show_message ("failure", `<p>Failed to submit review.</p>`);
    });
}

function activate (application_uuid) {
    jQuery("#submit-button").on("click", function(event) {
        event.preventDefault();
        event.stopPropagation();
        submit_application (application_uuid);
    });

}
