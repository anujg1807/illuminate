import pytest

from illuminate.classifier import MATCH, MAYBE, NO, classify

CASES = [
    # Direct titles
    ("Product Manager", "Copeland", MATCH),
    ("AI Product Manager (ID # 894)", "Aviate", MATCH),
    ("Principal Product Manager", "Microsoft", MATCH),
    ("Sr. Product Manager - Payments", "Razorpay", MATCH),
    ("Associate Product Manager", "Flipkart", MATCH),
    ("Group Product Manager, Growth", "Swiggy", MATCH),
    ("Technical Product Manager", "Atlassian", MATCH),
    ("Product Mgr II", "Amazon", MATCH),
    ("Product Management Intern", "CRED", MATCH),
    ("Senior Manager - Product Management", "Paytm", MATCH),
    ("Product Manager / Program Manager", "Acme", MATCH),
    # Indirect titles
    ("Head of Product", "Zepto", MATCH),
    ("Director, Product", "Freshworks", MATCH),
    ("VP - Product & Growth", "Meesho", MATCH),
    ("Vice President of Product", "PhonePe", MATCH),
    ("Chief Product Officer", "Groww", MATCH),
    ("Product Lead", "Zerodha", MATCH),
    ("Senior Manager, Product", "Myntra", MATCH),
    ("Manager - Products (Lending)", "KreditBee", MATCH),
    ("Product Head - Credit Cards", "HDFC Bank", MATCH),
    # Ambiguous
    ("Lead Product Owner", "Boeing", MATCH),
    ("Technical Product Owner, Smart Factory Solutions", "Magna International", MATCH),
    ("Senior Technical PM, Applied AI", "Netomi", MAYBE),
    ("APM - Growth", "Unacademy", MAYBE),
    ("Product Manager - Cardiology", "Sun Pharmaceutical Industries", MAYBE),
    ("Product Manager", "Cipla Pharma", MAYBE),
    ("Product Manager - Sales (Home Loans)", "Some Bank", MAYBE),
    ("Product - Growth", "Stealth Startup", MAYBE),
    ("Founding Product", "Stealth", MAYBE),
    # Other functions
    ("Product Marketing Manager", "Adobe", NO),
    ("Project Manager", "L&T", NO),
    ("Senior Digital Project Manager", "Publicis", NO),
    ("Technical Program Manager", "Google", NO),
    ("Production Manager", "Tata Steel", NO),
    ("Product Designer", "Figma", NO),
    ("Lead Product Designer", "Swiggy", NO),
    ("Head of Product Design", "Cred", NO),
    ("Manager - Product Support", "Zoho", NO),
    ("Product Engineer(Adv+Elite) Development Engineering-Husky (India), Chennai", "Husky", NO),
    ("Product Engineering Architect", "Dell", NO),
    ("Product Analyst", "Uber", NO),
    ("Founder's Office (ID # 927)", "Aviate", NO),
    ("Technical Lead Engineer - Cybersecurity", "Aptiv", NO),
    ("UX Lead (Shopify Braze Figma CRO Ecommerce)", "Agency", NO),
    ("Manager, Engineering", "Stripe", NO),
    ("Assistant Project Manager (APM)", "Builder Co", NO),
    ("Customer Support - 2 PM shift", "BPO", NO),
    ("IN_Manager_SAP PM_Enterprise Apps SAP_Advisory_ Mumbai", "PwC India", NO),
    ("Oracle Sales Performance Management (SPM)", "Artech", NO),
    ("Product Line Management III", "Applied Materials India", MAYBE),
]


@pytest.mark.parametrize("title,company,expected", CASES)
def test_classify(title, company, expected):
    verdict = classify(title, company)
    assert verdict.label == expected, f"{title!r} -> {verdict}"
    assert verdict.reason
