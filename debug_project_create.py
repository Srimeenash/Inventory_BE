from users.models import User
from rest_framework.test import APIRequestFactory
from projects.views import ProjectViewSet
from projects.models import Project

user = User.objects.filter(is_active=True).first()
print('user', user)
if not user:
    raise SystemExit('No active user found')

payload = {
    'project_code': 'PRJ_TEST2',
    'name': 'Test2',
    'description': 'Desc',
    'department': 'R&D',
    'status': 'PLANNED',
    'budget': '123.45',
    'start_date': '2026-06-19',
    'end_date': '2026-06-30',
    'manager_id': user.id,
    'team_ids': [user.id],
}

view = ProjectViewSet.as_view({'post': 'create'})
request = APIRequestFactory().post('/api/projects/projects/', payload, format='json')
response = view(request)
print('status', response.status_code)
print('data', response.data)
print('count', Project.objects.filter(project_code='PRJ_TEST2').count())
