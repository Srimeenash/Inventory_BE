from django.http import JsonResponse

def home(request):
    return JsonResponse({
        "message": "IPMS Backend Running Successfully 🚀"
    })