"""Create a real applicant through the onboarding routes using fixture data."""

PASSWORD = "chosen-physician-password-1"


def submit_physician_application(client, email, password=PASSWORD):
    response = client.post("/api/onboarding/self-serve", json={"email": email})
    assert response.status_code == 200, response.text
    token = response.json()["onboarding_url"].rsplit("/", 1)[-1]

    def post(path, **payload):
        result = client.post("/api/onboarding/" + path, json={"token": token, **payload})
        assert result.status_code == 200, result.text
        return result.json()

    post("step1-identity", first_name="Amara", last_name="Okafor", email=email, password=password)
    team = client.app.state.team_store
    invite = team.get_health_system_by_onboarding_token(token)
    # Supply a known challenge; the real verification endpoint must consume it.
    team.create_otp_challenge(invite["id"], email, "123456")
    post("verify-otp", code="123456")
    cv = b"Amara Okafor, MD\nNephrology\nFellowship, Nephrology, Example University, 2012-2015\n"
    uploaded = client.post("/api/onboarding/asclepius/cv", data={"token": token},
                           files={"file": ("fixture-cv.txt", cv, "text/plain")})
    assert uploaded.status_code == 200, uploaded.text
    status = client.get("/api/onboarding/asclepius/cv/status", params={"token": token})
    assert status.status_code == 200 and status.json()["uploaded"]
    post("asclepius/credentials", credentials={
        "fullLegalName": "Amara Okafor", "primarySpecialty": "Nephrology", "degree": "MD"})
    post("asclepius/attestations", attestations={
        "consentCredentialShare": True, "attestIndependentJudgment": True,
        "ipAssignment": True, "noPhi": True, "signedInitials": "AO"})
    finished = post("asclepius/finish")
    assert finished["awaiting_review"] and finished["token"]
    user = client.app.state.asclepius_store.get_user_by_email(email)
    assert user["verification_status"] == "pending" and user["tier"] is None
    return user
