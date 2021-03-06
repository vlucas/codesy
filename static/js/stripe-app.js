$(window).load(function () {
    Stripe.setPublishableKey($('#codesy-html').data('stripe_key'));

    var codesy = {user:{}}
    if ($("#codesy_user_id").length > 0) {
    codesy.user.id = $("#codesy_user_id").val();
    }

    stripeResponse = function (csrf_token) {
        this.csrf_token = csrf_token
        return (function(_this){
            return function (status, response) {
                if (response.error) {
                    console.error("Stripe failed to tokenize");
                    $('#payment-errors').text(response.error.message);
                    document.location.reload();
                } else {
                    var token = response.id;
                    $.ajax({
                        method: "PATCH",
                        url: "/users/"+codesy.user.id + "/",
                        beforeSend: function(xhr, settings) {
                            xhr.setRequestHeader('X-CSRFToken', _this.csrf_token);
                        },
                        data: {
                        stripe_cc_token: token
                        },
                        success: function(data, status, jqXHR) {
                            console.log("Updated user.");
                        },
                        error: function(err) {
                            console.error("Error updating user.");
                            console.error(err);
                        },
                        complete: function(){
                            document.location.reload();
                        }
                    });
                }
            }
        })(this)
    }

    $('#cc-submit').click(function (e) {
        e.preventDefault();
        $('#cc-submit').text('Authorizing ... ');
        var payload = {
            number: $('#cc-number').val(),
            exp_month: $('#cc-ex-month').val(),
            exp_year: $('#cc-ex-year').val(),
            cvc: $('#cvc').val()
        };
        handleResponse = new stripeResponse($('form input[name="csrfmiddlewaretoken"]').val())
        // Create credit card
        Stripe.card.createToken(payload, handleResponse);
    });

})
