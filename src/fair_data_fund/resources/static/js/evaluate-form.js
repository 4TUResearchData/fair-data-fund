function gather_form_data () {
    return {
        "refinement": jQuery("#score-refinement").val(),
        "findable": jQuery("#score-findable").val(),
        "accessible": jQuery("#score-accessible").val(),
        "interoperable": jQuery("#score-interoperable").val(),
        "reusable": jQuery("#score-reusable").val(),
        "budget": jQuery("#score-budget").val(),
        "achievement": jQuery("#score-achievement").val()
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
