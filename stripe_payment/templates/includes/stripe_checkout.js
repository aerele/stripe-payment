// Embedded Elements checkout using PaymentIntents + PaymentElement.
// Flow: create an (unconfirmed) PaymentIntent on the server -> mount the
// PaymentElement with its client_secret -> confirm client-side (handles 3DS)
// -> hand the confirmed PaymentIntent id back to the server to finalize.

var stripe = Stripe("{{ publishable_key }}");

var checkoutData = {
	data: JSON.stringify({{ frappe.form_dict|json }}),
	reference_doctype: {{ reference_doctype | tojson }},
	reference_docname: {{ reference_docname | tojson }},
	payment_gateway: {{ payment_gateway | tojson }}
};

var elements;
var paymentElement;
var clientSecret;
var paymentIntentId;

function showError(message) {
	var displayError = document.getElementById('card-errors');
	if (displayError) {
		displayError.textContent = message || '';
	}
}

function setSubmitting(isSubmitting) {
	if (isSubmitting) {
		$('#submit').prop('disabled', true).html(__('Processing...'));
	} else {
		$('#submit').prop('disabled', false).html(__('Pay') + ' {{ amount }}');
	}
}

function redirectAfter(result) {
	setTimeout(function () {
		if (result && result.redirect_to) {
			window.location.href = result.redirect_to;
		}
	}, 2000);
}

function mountPaymentElement() {
	frappe.call({
		method: "stripe_payment.templates.pages.stripe_checkout.create_payment_intent",
		freeze: true,
		headers: { "X-Requested-With": "XMLHttpRequest" },
		args: {
			data: checkoutData.data,
			reference_doctype: checkoutData.reference_doctype,
			reference_docname: checkoutData.reference_docname,
			payment_gateway: checkoutData.payment_gateway
		},
		callback: function (r) {
			if (!r.message || !r.message.client_secret) {
				showError(__('Could not initialise the payment. Please try again.'));
				return;
			}
			clientSecret = r.message.client_secret;
			paymentIntentId = r.message.payment_intent;
			elements = stripe.elements({ clientSecret: clientSecret });
			paymentElement = elements.create('payment', {
				defaultValues: {
					billingDetails: {
						name: {{ payer_name | tojson }},
						email: {{ payer_email | tojson }}
					}
				}
			});
			paymentElement.mount('#card-element');
		}
	});
}

function confirmPayment() {
	if (!elements || !clientSecret) {
		showError(__('Payment is still initialising. Please wait a moment.'));
		return;
	}
	setSubmitting(true);
	recordConsentThen(doConfirm);
}

function recordConsentThen(next) {
	// Record save-card consent before confirming, so cards store only on opt-in.
	if (!$('#save-card').is(':checked') || !paymentIntentId) {
		next();
		return;
	}
	frappe.call({
		method: "stripe_payment.templates.pages.stripe_checkout.set_card_consent",
		headers: { "X-Requested-With": "XMLHttpRequest" },
		args: {
			payment_intent: paymentIntentId,
			client_secret: clientSecret,
			reference_doctype: checkoutData.reference_doctype,
			reference_docname: checkoutData.reference_docname,
			payment_gateway: checkoutData.payment_gateway
		},
		// Don't block payment if the consent write fails; the card just won't be saved.
		always: function () { next(); }
	});
}

function doConfirm() {
	stripe.confirmPayment({
		elements: elements,
		redirect: 'if_required',
		confirmParams: {
			receipt_email: $('input[name=cardholder-email]').val(),
			// Redirect methods (e.g. UPI) return here; card / inline 3DS resolve without it.
			return_url: window.location.href.split('#')[0]
		}
	}).then(function (result) {
		if (result.error) {
			showError(result.error.message);
			$('.error').show();
			setSubmitting(false);
			return;
		}

		var intent = result.paymentIntent;
		if (intent && (intent.status === 'succeeded' || intent.status === 'processing')) {
			finalizeIntent(intent);
		} else {
			showError(__('The payment could not be completed.'));
			setSubmitting(false);
		}
	});
}

function finalizeIntent(intent) {
	// Hand the confirmed/processing intent to the server to settle; the
	// payment_intent.succeeded webhook is the authoritative backstop.
	frappe.call({
		method: "stripe_payment.templates.pages.stripe_checkout.make_payment",
		freeze: true,
		headers: { "X-Requested-With": "XMLHttpRequest" },
		args: {
			payment_intent: intent.id,
			data: checkoutData.data,
			reference_doctype: checkoutData.reference_doctype,
			reference_docname: checkoutData.reference_docname,
			payment_gateway: checkoutData.payment_gateway
		},
		callback: function (r) {
			var msg = r.message || {};
			$('#submit').hide();
			if (msg.status === "Completed" || msg.status === "Pending") {
				// Pending = async method (UPI/ACH/SEPA) accepted and still settling, not a failure.
				$('.success').show();
			} else {
				$('.error').show();
			}
			redirectAfter(msg);
		},
		error: function () {
			// Payment already captured; keep the button hidden to avoid a double-charge.
			$('#submit').hide();
			showError(__('Your payment was received and is being processed. Please do not pay again — if anything looks wrong, contact us.'));
			$('.error').hide();
		}
	});
}

function handleRedirectReturn(secret) {
	// A redirect method (e.g. UPI) sent the buyer back here. Retrieve the intent and
	// hand it to the server; it may be succeeded or still processing.
	setSubmitting(true);
	stripe.retrievePaymentIntent(secret).then(function (result) {
		var intent = result.paymentIntent;
		if (intent && (intent.status === 'succeeded' || intent.status === 'processing')) {
			finalizeIntent(intent);
		} else {
			showError(__('The payment could not be completed.'));
			setSubmitting(false);
		}
	});
}

frappe.ready(function () {
	var returnedSecret = new URLSearchParams(window.location.search).get('payment_intent_client_secret');
	if (returnedSecret) {
		handleRedirectReturn(returnedSecret);
		return;
	}
	mountPaymentElement();
	$('#submit').off("click").on("click", function (e) {
		e.preventDefault();
		confirmPayment();
	});
});
