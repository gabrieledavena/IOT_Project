from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from .models import Customer, Community

class CustomerRegistrationForm(UserCreationForm):
    name = forms.CharField(max_length=100, required=True)
    surname = forms.CharField(max_length=100, required=True)
    community = forms.ModelChoiceField(queryset=Community.objects.all(), required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = UserCreationForm.Meta.fields + ('name', 'surname', 'community',)

    def save(self, commit=True):
        user = super().save(commit=False)
        if commit:
            user.save()
            Customer.objects.create(
                user=user,
                name=self.cleaned_data.get('name'),
                surname=self.cleaned_data.get('surname'),
                community=self.cleaned_data.get('community'),
            )
        return user
